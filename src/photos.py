"""Many overlapping photos -> one picture on the ground, without photogrammetry.

A farmer's drone writes a folder of photos, each tagged with where the drone
was. OpenDroneMap turns those into a map and a height model, but it needs
Docker and hours. This is the quick way, and the one that always runs:

1. **Place every photo from its tags.** The GPS gives the middle, the height
   above takeoff and the camera give how many centimetres a pixel covers, and
   the gimbal's heading gives which way is up. Each placement is a similarity:
   a scale, a turn and a shift, which on the complex plane is ``z * p + t``.
2. **Match neighbours.** SIFT features in each photo, matched against its
   nearest photos by GPS; a pair that agrees on a scale-and-turn (RANSAC) with
   enough points is tied together at those points.
3. **Solve everything at once.** Every tie says two photos put the same point
   on the same spot of ground; every GPS tag says roughly where a photo's
   middle is. Both are linear in ``z`` and ``t``, so one sparse least-squares
   solve places the whole flight, with the ties deciding how photos fit each
   other and the GPS deciding where the whole lot sits on the Earth.
4. **Paint it.** Every spot of ground takes its colour from the photo that saw
   it closest to straight down (the one whose middle is nearest), which keeps
   seams where tall plants lean least.

What this cannot do, and says so: it has no height model, so a flat picture is
all it makes (plant height needs ODM or a laser), and it treats the ground as
flat, so on a steep field the joins can be off by a little. Placing is only as
good as the GPS for where the whole map sits: a consumer drone's few metres.

Thermal frames go through the same placing, then are written one by one as
north-up GeoTIFFs, so the thermal mosaic (levelling, warming trend, leaves)
works on them exactly as on a scanner's frames.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Sequence

import numpy as np

from . import ingest

log = logging.getLogger(__name__)

#: Photos are matched at this width; full size adds time and nothing to the fit.
MATCH_WIDTH_PX = 1200
SIFT_FEATURES = 4000
#: A colour photo whose grey spreads less than this gets its local contrast lifted.
FLAT_GREY_STD = 18.0
#: Each photo is tried against this many of its nearest photos by GPS.
NEIGHBOURS = 8
#: A pair needs this many points agreeing on one scale-and-turn to count.
MIN_INLIERS = 20
#: Once placed, photos whose footprints overlap by this share are matched too.
MIN_OVERLAP_SHARE = 0.10
#: How far a match may disagree with the placing so far, as a share of a
#: photo's width: loose while only the tags place photos, tight once solved.
FIRST_TOLERANCE = 0.6
SECOND_TOLERANCE = 0.15
#: Ties kept per pair: enough to fix it, few enough to keep the solve small.
TIES_PER_PAIR = 40
#: How far one photo's GPS wanders from the next within a flight, in metres (the
#: whole flight's offset, often bigger, moves the map but never turns it).
GPS_NOISE_M = 1.0
#: A group is turned and resized onto its GPS only when that turn is known to
#: about this many degrees; otherwise it is only shifted.
MAX_TURN_ERROR_DEG = 2.0
#: A pair whose ties disagree with the solution by more than this is dropped.
PAIR_OUTLIER_M = 0.5
#: The painted map is kept under this many pixels a side.
MAX_OUTPUT_PX = 8000
PHOTO_SUFFIXES = (".jpg", ".jpeg", ".tif", ".tiff", ".png")


class StitchError(RuntimeError):
    """Raised when a set of photos cannot be placed, with what would fix it."""


@dataclass
class Photo:
    """One photo and where it lies: ground = z * p + t, p its pixel (y up, from the middle)."""

    path: Path
    width_px: int
    height_px: int
    east: float
    north: float
    taken: datetime | None
    z: complex
    t: complex
    tagged: bool                    # height, camera and heading all came with the photo
    connected: bool = False         # tied to at least one other photo
    z0: complex = 0j                # the scale-and-turn from the tags, before solving

    def pixel(self, col: np.ndarray, row: np.ndarray) -> np.ndarray:
        """Pixel positions as complex numbers, middle at 0 and y pointing up."""
        return (col - self.width_px / 2) + 1j * (self.height_px / 2 - row)

    def ground(self, col: np.ndarray, row: np.ndarray) -> np.ndarray:
        return self.z * self.pixel(col, row) + self.t

    @property
    def metres_per_px(self) -> float:
        return abs(self.z)

    def affine(self):
        """Pixel (col, row) to ground (east, north), as a rasterio Affine."""
        from affine import Affine

        a, b = self.z.real, self.z.imag
        w, h = self.width_px, self.height_px
        # east  = a*(col - w/2) - b*(h/2 - row) + tx
        # north = b*(col - w/2) + a*(h/2 - row) + ty
        return Affine(a, b, self.t.real - a * w / 2 - b * h / 2,
                      b, -a, self.t.imag - b * w / 2 + a * h / 2)


@dataclass
class Stitched:
    """How the placing went, for the report and for the farmer's page."""

    photos: list[Photo]
    crs: str
    pairs_tried: int = 0
    pairs_used: int = 0
    tie_rms_m: float | None = None
    moved_from_gps_m: float | None = None
    notes: list[str] = dc_field(default_factory=list)          # from reading the photos
    solve_notes: list[str] = dc_field(default_factory=list)    # from the latest solve

    @property
    def all_notes(self) -> list[str]:
        return self.notes + self.solve_notes

    @property
    def connected(self) -> int:
        return sum(p.connected for p in self.photos)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def find_photos(folder: Path) -> list[Path]:
    """Every photo under a folder, in name order (which is capture order for drones)."""
    return sorted(p for p in Path(folder).rglob("*")
                  if p.is_file() and p.suffix.lower() in PHOTO_SUFFIXES)


def read_xmp_number(path: Path, key: str) -> float | None:
    """A number from the XMP a DJI camera writes, e.g. ``GimbalYawDegree``."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(262144)
    except OSError:
        return None
    for marker in (f'drone-dji:{key}="'.encode(), f"<drone-dji:{key}>".encode()):
        start = head.find(marker)
        if start >= 0:
            chunk = head[start + len(marker):start + len(marker) + 24]
            text = chunk.decode("ascii", "ignore").split('"')[0].split("<")[0].strip()
            try:
                return float(text)
            except ValueError:
                return None
    return None


def utm_crs(lon: float, lat: float) -> str:
    zone = int((lon + 180) // 6) + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def read_photos(paths: Sequence[Path], *, height_m: float | None = None) -> Stitched:
    """Every photo with a GPS tag, placed from its tags alone.

    Args:
        height_m: flying height above the ground, for photos that do not say.
    """
    from pyproj import Transformer

    shots = []
    for path in paths:
        try:
            shot = ingest.read_shot(Path(path))
        except Exception as exc:  # noqa: BLE001 - one bad file must not sink a flight
            log.warning("skipping %s: %s", Path(path).name, exc)
            continue
        if shot.has_gps:
            shots.append(shot)
    if len(shots) < 2:
        raise StitchError(
            f"{len(shots)} of {len(paths)} photo(s) carry a GPS position, and placing them "
            "needs at least two. Photos straight off the drone keep their positions; "
            "ones sent through a chat app or edited usually lose them.")
    shots.sort(key=lambda s: (s.timestamp or datetime.min, s.path.name))
    lon0 = float(np.median([s.lon for s in shots]))
    lat0 = float(np.median([s.lat for s in shots]))
    crs = utm_crs(lon0, lat0)
    to_utm = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    easts, norths = to_utm.transform([s.lon for s in shots], [s.lat for s in shots])
    centres = np.array(easts) + 1j * np.array(norths)

    stitched = Stitched([], crs)
    spacing = _typical_spacing(centres)
    for index, shot in enumerate(shots):
        height = shot.relative_alt_m or height_m
        per_px = (ingest.estimate_gsd_cm(shot, height) / 100.0
                  if height and ingest.estimate_gsd_cm(shot, height) else None)
        yaw = read_xmp_number(shot.path, "GimbalYawDegree")
        if yaw is None:
            yaw = read_xmp_number(shot.path, "FlightYawDegree")
        tagged = per_px is not None and yaw is not None
        if per_px is None:
            # Unknown size: guess that neighbouring photos overlap by about
            # two thirds, and let the ties and the GPS spacing settle the rest.
            per_px = 3.0 * spacing / max(shot.width_px, shot.height_px)
        if yaw is None:
            yaw = _track_heading(centres, index)
        # Camera top pointing ``yaw`` degrees clockwise from north.
        z = per_px * complex(math.cos(math.radians(yaw)), -math.sin(math.radians(yaw)))
        stitched.photos.append(Photo(
            path=shot.path, width_px=shot.width_px, height_px=shot.height_px,
            east=float(centres[index].real), north=float(centres[index].imag),
            taken=shot.timestamp, z=z, t=complex(centres[index]), tagged=tagged, z0=z))
    untagged = sum(not p.tagged for p in stitched.photos)
    if untagged:
        stitched.notes.append(
            f"{untagged} photo(s) did not say their height or heading; those were worked "
            "out from how the photos overlap instead.")
    if len(shots) < len(paths):
        stitched.notes.append(f"{len(paths) - len(shots)} file(s) had no GPS position and "
                              "were left out.")
    return stitched


def _typical_spacing(centres: np.ndarray) -> float:
    """Median distance from each photo to its nearest neighbour, in metres."""
    if len(centres) < 2:
        return 10.0
    distances = np.abs(centres[:, None] - centres[None, :])
    np.fill_diagonal(distances, np.inf)
    return float(max(np.median(distances.min(axis=1)), 0.5))


def _track_heading(centres: np.ndarray, index: int) -> float:
    """Which way the drone was flying at a photo, in degrees from north."""
    before = centres[max(index - 1, 0)]
    after = centres[min(index + 1, len(centres) - 1)]
    step = after - before
    if abs(step) < 1e-6:
        return 0.0
    return math.degrees(math.atan2(step.real, step.imag))


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


def _for_matching(path: Path) -> tuple[np.ndarray, float]:
    """A grey picture for feature matching, and how much it was shrunk."""
    import cv2

    image = _read_any(path)
    if image.ndim == 3:
        grey = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        if float(grey.std()) < FLAT_GREY_STD:
            # Local contrast lifted, but only for a flat picture: a closed canopy
            # is one even green, and soil and leaf can come out the same grey,
            # which leaves SIFT nothing to hold. On an ordinary photo it only
            # adds noise to match on.
            grey = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(grey)
    else:
        grey = image
    if grey.dtype != np.uint8:
        # A thermal frame: stretch its own range to 0-255 and lift the contrast,
        # since a canopy a few degrees either way is all a leaf pattern it has.
        values = grey.astype(np.float64)
        finite = values[np.isfinite(values)]
        low, high = (np.percentile(finite, [1, 99]) if finite.size else (0.0, 1.0))
        values = np.clip((np.nan_to_num(values, nan=low) - low) / max(high - low, 1e-6), 0, 1)
        grey = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(
            (values * 255).astype(np.uint8))
    scale = min(1.0, MATCH_WIDTH_PX / max(grey.shape))
    if scale < 1.0:
        grey = cv2.resize(grey, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return grey, scale


def _read_any(path: Path) -> np.ndarray:
    """A photo as an array: RGB for colour pictures, one band as it was for thermal."""
    import cv2

    if Path(path).suffix.lower() in (".tif", ".tiff"):
        import rasterio

        with rasterio.open(path) as dataset:
            data = dataset.read()
        if data.shape[0] >= 3 and data.dtype == np.uint8:
            return np.transpose(data[:3], (1, 2, 0))
        return data[0]
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise StitchError(f"{Path(path).name} cannot be read as a picture")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def features_of(stitched: Stitched) -> list[tuple[np.ndarray, np.ndarray | None]]:
    """SIFT points (full-size pixel positions) and their descriptors, per photo."""
    import cv2

    sift = cv2.SIFT_create(nfeatures=SIFT_FEATURES)
    features = []
    for photo in stitched.photos:
        grey, scale = _for_matching(photo.path)
        keypoints, descriptors = sift.detectAndCompute(grey, None)
        points = (np.array([k.pt for k in keypoints], dtype=np.float64) / scale
                  if keypoints else np.zeros((0, 2)))
        features.append((points, descriptors))
    return features


def nearest_pairs(stitched: Stitched) -> set[tuple[int, int]]:
    """Each photo with its nearest photos by GPS: the first guess at who overlaps whom."""
    centres = np.array([p.t for p in stitched.photos])
    pairs: set[tuple[int, int]] = set()
    for i in range(len(centres)):
        for j in np.argsort(np.abs(centres - centres[i]))[1:NEIGHBOURS + 1]:
            pairs.add((min(i, int(j)), max(i, int(j))))
    return pairs


def overlapping_pairs(stitched: Stitched, *, min_share: float = MIN_OVERLAP_SHARE
                      ) -> set[tuple[int, int]]:
    """Every pair whose footprints, as placed now, overlap by at least ``min_share``.

    Nearest-by-GPS misses neighbours across flight lines when photos along a
    line are much closer together than the lines are, which leaves each line
    to drift on its own; this finds them once the photos are roughly placed.
    """
    from shapely import STRtree
    from shapely.geometry import Polygon

    shapes = [Polygon([(c.real, c.imag) for c in _footprint(p)]) for p in stitched.photos]
    tree = STRtree(shapes)
    pairs: set[tuple[int, int]] = set()
    for i, shape in enumerate(shapes):
        for j in tree.query(shape):
            j = int(j)
            if j <= i:
                continue
            share = shape.intersection(shapes[j]).area / min(shape.area, shapes[j].area)
            if share >= min_share:
                pairs.add((i, j))
    return pairs


def match_pairs(stitched: Stitched, features: list, pairs: set[tuple[int, int]], *,
                tolerance: float | None) -> list[tuple[int, int, np.ndarray, np.ndarray]]:
    """Ties for the given pairs: (i, j, points in i, same points in j).

    Points are complex pixel positions in each photo's own full-size frame. A
    match has to agree with where the photos are placed now to within
    ``tolerance`` (a share of the footprint): in a field of identical rows a
    wrong match one row over can look every bit as good as the right one.
    None skips that check, for photos placed so far only by a guess.
    """
    import cv2

    photos = stitched.photos
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    ties = []
    rng = np.random.default_rng(0)
    for i, j in sorted(pairs):
        (points_i, desc_i), (points_j, desc_j) = features[i], features[j]
        if (desc_i is None or desc_j is None or len(desc_i) < MIN_INLIERS
                or len(desc_j) < MIN_INLIERS):
            continue
        good = [m for m, n in (pair for pair in matcher.knnMatch(desc_i, desc_j, k=2)
                               if len(pair) == 2) if m.distance < 0.75 * n.distance]
        if len(good) < MIN_INLIERS:
            continue
        a = points_i[[m.queryIdx for m in good]]
        b = points_j[[m.trainIdx for m in good]]
        model, inliers = cv2.estimateAffinePartial2D(
            a, b, method=cv2.RANSAC, ransacReprojThreshold=4.0 / min(
                1.0, MATCH_WIDTH_PX / max(photos[i].width_px, photos[i].height_px)),
            maxIters=4000, confidence=0.995)
        if model is None or inliers is None or int(inliers.sum()) < MIN_INLIERS:
            continue
        scale = math.hypot(model[0, 0], model[1, 0])
        # Two photos from one flight see the ground at nearly one size.
        expected = abs(photos[i].z) / abs(photos[j].z)
        if not 0.6 < scale * expected < 1.6:
            continue
        keep = np.flatnonzero(inliers.ravel())
        if len(keep) > TIES_PER_PAIR:
            keep = rng.choice(keep, TIES_PER_PAIR, replace=False)
        p = photos[i].pixel(a[keep, 0], a[keep, 1])
        q = photos[j].pixel(b[keep, 0], b[keep, 1])
        # Where the placing so far puts these points, seen from each photo.
        gap = float(np.median(np.abs(photos[i].z * p + photos[i].t
                                     - photos[j].z * q - photos[j].t)))
        reach = max(photos[i].width_px, photos[i].height_px) * abs(photos[i].z)
        if tolerance is not None and gap > tolerance * reach:
            continue
        ties.append((i, j, p, q))
    return ties


# --------------------------------------------------------------------------- #
# Solving
# --------------------------------------------------------------------------- #


def _groups(n: int, ties: list) -> list[list[int]]:
    """Photos that are tied to each other, directly or through others."""
    parent = list(range(n))

    def root(k: int) -> int:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i, j, _, _ in ties:
        parent[root(i)] = root(j)
    members: dict[int, list[int]] = {}
    for k in range(n):
        members.setdefault(root(k), []).append(k)
    return list(members.values())


def _relative(photos: list[Photo], ties: list) -> None:
    """Place tied photos against each other, one photo per group held still.

    Ties alone cannot tell a map from a smaller copy of it (every tie is met
    by shrinking everything to a point), so one photo in each group keeps the
    placement its tags gave it and the rest are placed relative to it. Where
    the group sits, how it is turned and how big it is comes after, from GPS.
    A few hundred photos make a small system, so it is solved directly.
    """
    from scipy.sparse import coo_matrix

    n = len(photos)
    degree = np.zeros(n, dtype=int)
    for i, j, p, _ in ties:
        degree[i] += len(p)
        degree[j] += len(p)
    known: dict[int, float] = {}
    for group in _groups(n, ties):
        for k in group if len(group) == 1 else [max(group, key=lambda m: degree[m])]:
            photo = photos[k]
            for offset, value in enumerate((photo.z0.real, photo.z0.imag,
                                            photo.t.real, photo.t.imag)):
                known[4 * k + offset] = value
    free = [c for c in range(4 * n) if c not in known]
    column = {c: index for index, c in enumerate(free)}

    rows, cols, values, rhs = [], [], [], []

    def equation(entries: list[tuple[int, float]]) -> None:
        # Unknowns per photo k: a, b, tx, ty at 4k..4k+3; ground = (a + ib) p + (tx + i ty).
        row, target = len(rhs), 0.0
        for c, value in entries:
            if c in known:
                target -= value * known[c]
            else:
                rows.append(row)
                cols.append(column[c])
                values.append(value)
        rhs.append(target)

    for i, j, p, q in ties:
        for pi, qj in zip(p, q):
            equation([(4 * i, pi.real), (4 * i + 1, -pi.imag), (4 * i + 2, 1.0),
                      (4 * j, -qj.real), (4 * j + 1, qj.imag), (4 * j + 2, -1.0)])
            equation([(4 * i, pi.imag), (4 * i + 1, pi.real), (4 * i + 3, 1.0),
                      (4 * j, -qj.imag), (4 * j + 1, -qj.real), (4 * j + 3, -1.0)])
    solution = dict(known)
    if free:
        matrix = coo_matrix((values, (rows, cols)), shape=(len(rhs), len(free))).tocsc()
        normal = (matrix.T @ matrix).toarray()
        solved = np.linalg.lstsq(normal, matrix.T @ np.array(rhs), rcond=None)[0]
        solution.update({c: float(solved[column[c]]) for c in free})
    for k, photo in enumerate(photos):
        photo.z = complex(solution[4 * k], solution[4 * k + 1])
        photo.t = complex(solution[4 * k + 2], solution[4 * k + 3])


def _onto_gps(photos: list[Photo], group: list[int], gps: np.ndarray) -> str:
    """Move, turn and size one placed group so its photo middles meet their GPS.

    A similarity on the complex plane: gps ~ s * t + c. Where the photos are too
    few or too close together for the GPS to settle the turn, only the shift is taken.
    """
    middles = np.array([photos[k].t for k in group])
    targets = gps[group]
    # How well the GPS pins the turn: its wander over how widely, and how many
    # times, the group's photos are spread.
    spread = float(np.sqrt(np.mean(np.abs(targets - targets.mean()) ** 2)))
    turn_error = math.degrees(math.atan2(GPS_NOISE_M, spread * math.sqrt(len(group))))
    if len(group) >= 3 and turn_error <= MAX_TURN_ERROR_DEG:
        design = np.column_stack([middles, np.ones(len(group))])
        (s, c), *_ = np.linalg.lstsq(design, targets, rcond=None)
        how = "shift, turn and size"
    else:
        s, c = 1.0 + 0j, complex(np.mean(targets - middles))
        how = "shift only"
    for k in group:
        photos[k].t = s * photos[k].t + c
        photos[k].z = s * photos[k].z
    return how


def solve(stitched: Stitched, ties: list, *, rounds: int = 3) -> None:
    """Place every photo: against its neighbours first, then all together onto the GPS.

    Pairs whose ties disagree with the rest (a false match, a moving tractor)
    are dropped and the placing done again.
    """
    photos = stitched.photos
    gps = np.array([complex(p.east, p.north) for p in photos])     # as tagged, every time
    kept = list(ties)
    for _ in range(rounds):
        for photo, start in zip(photos, gps):
            photo.z, photo.t = photo.z0, start
        _relative(photos, kept)
        errors = [float(np.sqrt(np.mean(np.abs(photos[i].z * p + photos[i].t
                                                - photos[j].z * q - photos[j].t) ** 2)))
                  for i, j, p, q in kept]
        if not errors:
            break
        limit = max(PAIR_OUTLIER_M, 5 * float(np.median(errors)))
        survivors = [tie for tie, error in zip(kept, errors) if error <= limit]
        if len(survivors) == len(kept):
            break
        log.info("dropped %d pair(s) that disagreed with the rest", len(kept) - len(survivors))
        kept = survivors

    groups = sorted(_groups(len(photos), kept), key=len, reverse=True)
    correction = None
    for group in groups:
        how = _onto_gps(photos, group, gps)
        if correction is None and how == "shift, turn and size":
            # How far the tags' scale and heading were from what the biggest group
            # showed: a camera mounted sideways, a height from the wrong takeoff.
            k = group[0]
            correction = photos[k].z / photos[k].z0
            reference = complex(np.median([photos[m].z.real for m in group]),
                                np.median([photos[m].z.imag for m in group]))
        elif correction is not None and how == "shift only":
            # A photo or small group the GPS could not turn or size. Tagged
            # photos get the correction the rest of the flight needed; untagged
            # ones, whose heading was only a guess, are turned as a block to
            # match the flight's typical photo instead.
            middle = np.mean([photos[k].t for k in group])
            if all(photos[k].tagged for k in group):
                turn = correction
            else:
                turn = reference / photos[group[0]].z
            for k in group:
                photos[k].z = photos[k].z * turn
                photos[k].t = middle + (photos[k].t - middle) * (turn / abs(turn))
    for photo in photos:
        photo.connected = False
    residuals = []
    for i, j, p, q in kept:
        photos[i].connected = photos[j].connected = True
        residuals.append(np.abs(photos[i].z * p + photos[i].t - photos[j].z * q - photos[j].t))
    stitched.pairs_used = len(kept)
    stitched.tie_rms_m = (float(np.sqrt(np.mean(np.concatenate(residuals) ** 2)))
                          if residuals else None)
    stitched.moved_from_gps_m = float(np.median(np.abs(np.array([p.t for p in photos]) - gps)))
    stitched.solve_notes = []
    lonely = len(photos) - stitched.connected
    if lonely:
        stitched.solve_notes.append(
            f"{lonely} photo(s) matched no neighbour and sit where their GPS put them, which "
            "can be a few metres off. More overlap between photos (70% or more) fixes that.")
    big = [g for g in groups if len(g) > 1]
    if len(big) > 1:
        stitched.solve_notes.append(
            f"The photos fell into {len(big)} groups that share no matching points, each placed "
            "on the GPS by itself; where they meet, the joins can be a few metres out.")


def place(paths: Sequence[Path], *, height_m: float | None = None) -> Stitched:
    """Read, match and solve: every photo placed on the ground."""
    stitched = read_photos(paths, height_m=height_m)
    features = features_of(stitched)
    # First the nearest photos by GPS, loosely checked (the tags may be well
    # off); then, with everything roughly placed, every pair that overlaps,
    # checked tightly; then the whole flight solved again with all of them.
    tried = nearest_pairs(stitched)
    guessed = not all(photo.tagged for photo in stitched.photos)
    ties = match_pairs(stitched, features, tried,
                       tolerance=None if guessed else FIRST_TOLERANCE)
    solve(stitched, ties)
    more = overlapping_pairs(stitched) - tried
    if more:
        tried |= more
        ties += match_pairs(stitched, features, more, tolerance=SECOND_TOLERANCE)
        solve(stitched, ties)
    stitched.pairs_tried = len(tried)
    if not ties:
        stitched.solve_notes.append(
            "No two photos could be matched, so every photo sits where its GPS put it. "
            "The picture will show seams; flying with more overlap fixes that.")
    log.info("placed %d photo(s): %d pair(s) of %d tied, ties agree to %s m",
             len(stitched.photos), stitched.pairs_used, stitched.pairs_tried,
             f"{stitched.tie_rms_m:.3f}" if stitched.tie_rms_m is not None else "-")
    return stitched


# --------------------------------------------------------------------------- #
# Painting
# --------------------------------------------------------------------------- #


def _footprint(photo: Photo) -> np.ndarray:
    corners = photo.ground(np.array([0, photo.width_px, photo.width_px, 0], dtype=float),
                           np.array([0, 0, photo.height_px, photo.height_px], dtype=float))
    return corners


def paint(stitched: Stitched, out_path: Path, *, resolution_m: float | None = None) -> Path:
    """One colour map of the whole flight as a GeoTIFF (RGB plus a mask of where it has data)."""
    import cv2
    import rasterio
    from affine import Affine

    photos = stitched.photos
    corners = np.concatenate([_footprint(p) for p in photos])
    west, east = corners.real.min(), corners.real.max()
    south, north = corners.imag.min(), corners.imag.max()
    finest = float(np.median([p.metres_per_px for p in photos]))
    resolution = max(resolution_m or finest,
                     (east - west) / MAX_OUTPUT_PX, (north - south) / MAX_OUTPUT_PX)
    width = int(math.ceil((east - west) / resolution))
    height = int(math.ceil((north - south) / resolution))
    out_transform = Affine(resolution, 0, west, 0, -resolution, north)

    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    best = np.zeros((height, width), dtype=np.float32)
    for photo in photos:
        image = _read_any(photo.path)
        if image.ndim != 3:
            raise StitchError(f"{photo.path.name} is not a colour photo")
        box = _footprint(photo)
        c0 = max(int((box.real.min() - west) / resolution) - 1, 0)
        c1 = min(int((box.real.max() - west) / resolution) + 2, width)
        r0 = max(int((north - box.imag.max()) / resolution) - 1, 0)
        r1 = min(int((north - box.imag.min()) / resolution) + 2, height)
        if c1 <= c0 or r1 <= r0:
            continue
        # Output pixel (within the window) -> ground -> this photo's pixel.
        window = out_transform * Affine.translation(c0, r0)
        to_source = ~photo.affine() * window
        matrix = np.array([[to_source.a, to_source.b, to_source.c],
                           [to_source.d, to_source.e, to_source.f]], dtype=np.float64)
        size = (c1 - c0, r1 - r0)
        warped = cv2.warpAffine(image, matrix, size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        cols, rows = np.meshgrid(np.arange(size[0]) + 0.5, np.arange(size[1]) + 0.5)
        src_c = matrix[0, 0] * cols + matrix[0, 1] * rows + matrix[0, 2]
        src_r = matrix[1, 0] * cols + matrix[1, 1] * rows + matrix[1, 2]
        # Nearest to the photo's middle wins: 1 at the middle, 0 at its edge.
        weight = 1.0 - np.maximum(np.abs(src_c / photo.width_px - 0.5),
                                  np.abs(src_r / photo.height_px - 0.5)) * 2
        weight = weight.astype(np.float32)
        take = weight > best[r0:r1, c0:c1]
        canvas[r0:r1, c0:c1][take] = warped[take]
        best[r0:r1, c0:c1][take] = weight[take]

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mask = np.where(best > 0, 255, 0).astype(np.uint8)
    with rasterio.open(out_path, "w", driver="GTiff", width=width, height=height, count=4,
                       dtype="uint8", crs=stitched.crs, transform=out_transform,
                       compress="deflate", tiled=True, photometric="RGB") as dataset:
        for band in range(3):
            dataset.write(canvas[:, :, band], band + 1)
        dataset.write(mask, 4)
        dataset.colorinterp = [rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
                               rasterio.enums.ColorInterp.blue, rasterio.enums.ColorInterp.alpha]
        dataset.update_tags(PROVENANCE="stitched from photos by GPS and feature matching, "
                                       "no height model")
    log.info("painted %d x %d px at %.1f cm", width, height, resolution * 100)
    return out_path


def camera_pattern(frames: list[np.ndarray], taken: list[datetime | None],
                   *, slice_s: float = 300.0) -> list[np.ndarray]:
    """Take the camera's own shading off each frame, as the camera saw it.

    A thermal camera reads a little warmer in one band of its picture and
    cooler towards its edges, and the band drifts as it warms up. Measured here
    as one profile down the picture and one across, per five minutes of flying,
    from the median of every frame taken then (anything in the field averages
    out over dozens of frames; the camera's pattern does not). This has to
    happen before frames are turned onto the map, while the pattern still sits
    in the same place in every frame.
    """
    shapes = [frame.shape for frame in frames]
    common = max(set(shapes), key=shapes.count)
    start = min((t for t in taken if t), default=None)
    seconds = np.array([(t - start).total_seconds() if t and start else 0.0 for t in taken])
    slots = np.floor(seconds / slice_s).astype(int)
    out = [frame.astype(np.float64) for frame in frames]
    for slot in np.unique(slots):
        members = [k for k in np.flatnonzero(slots == slot) if shapes[k] == common]
        if len(members) < 20:
            continue                    # too few frames to tell the camera from the field
        with np.errstate(invalid="ignore"):
            stack = np.stack([out[k] - np.nanmedian(out[k]) for k in members])
            pattern = np.nanmedian(stack, axis=0)
        down = np.nanmedian(pattern, axis=1)
        across = np.nanmedian(pattern - down[:, None], axis=0)
        correction = np.nan_to_num(down[:, None] + across[None, :])
        for k in members:
            out[k] = out[k] - correction
    return out


def write_frames(stitched: Stitched, folder: Path, *, flatten: bool = True) -> list[Path]:
    """Each thermal frame as its own north-up GeoTIFF, for the thermal mosaic.

    Values are kept as the camera wrote them (the mosaic works out the unit), and
    the capture time goes in the TIFF's own date tag so the warming trend can be
    followed through the flight.
    """
    import cv2
    import rasterio
    from affine import Affine

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    frames = [_read_any(photo.path) for photo in stitched.photos]
    if any(frame.ndim != 2 for frame in frames):
        raise StitchError("a thermal frame has one band; these photos have colour")
    if flatten:
        frames = camera_pattern(frames, [photo.taken for photo in stitched.photos])
    written = []
    for index, (photo, data) in enumerate(zip(stitched.photos, frames)):
        box = _footprint(photo)
        resolution = photo.metres_per_px
        west, north = box.real.min(), box.imag.max()
        width = int(math.ceil((box.real.max() - west) / resolution))
        height = int(math.ceil((north - box.imag.min()) / resolution))
        transform = Affine(resolution, 0, west, 0, -resolution, north)
        to_source = ~photo.affine() * transform
        matrix = np.array([[to_source.a, to_source.b, to_source.c],
                           [to_source.d, to_source.e, to_source.f]], dtype=np.float64)
        warped = cv2.warpAffine(data.astype(np.float32), matrix, (width, height),
                                flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=float("nan"))
        path = folder / f"{index:05d}_{photo.path.stem}.tif"
        with rasterio.open(path, "w", driver="GTiff", width=width, height=height, count=1,
                           dtype="float32", crs=stitched.crs, transform=transform,
                           nodata=float("nan")) as dataset:
            dataset.write(warped, 1)
            if photo.taken:
                dataset.update_tags(TIFFTAG_DATETIME=photo.taken.strftime("%Y:%m:%d %H:%M:%S"))
        written.append(path)
    return written
