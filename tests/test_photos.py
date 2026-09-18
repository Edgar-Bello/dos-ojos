"""Stitching loose photos: placed from their tags, tied by what they share, solved at once.

Every test cuts one made-up field (a random texture with rows on it) into
photos whose true placement is known, writes the tags a drone would, and
checks the stitcher puts them back.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import piexif
import pytest
import rasterio
from affine import Affine
from PIL import Image
from pyproj import Transformer

from dosojos_drone import ingest, mosaic, photos

CRS = "EPSG:32614"                       # the Rio Grande Valley's UTM zone
ORIGIN = (583_000.0, 2_900_000.0)        # a corner of the made-up field, in metres
GSD = 0.02                               # metres per pixel in each photo
W, H = 600, 450


def _field(rng: np.random.Generator, size_m: float = 40.0, per_px: float = 0.01) -> np.ndarray:
    """A field seen from above: soil texture with green rows and some blotches."""
    n = int(size_m / per_px)
    soil = cv2.GaussianBlur(rng.normal(120, 30, (n, n)).astype(np.float32), (0, 0), 3)
    image = np.stack([soil * 1.1, soil, soil * 0.8], axis=2)
    for x in range(10, n, 76):                      # rows 0.76 m apart
        image[:, x:x + 30] = (60, 140, 50)
    blotches = cv2.GaussianBlur(rng.normal(0, 40, (n, n)).astype(np.float32), (0, 0), 25)
    image += blotches[..., None] * 0.6
    image += rng.normal(0, 12, image.shape)
    return np.clip(image, 0, 255).astype(np.uint8)


def _dms(value: float):
    value = abs(value)
    d = int(value)
    m = int((value - d) * 60)
    s = (value - d - m / 60) * 3600
    return ((d, 1), (m, 1), (int(s * 10000), 10000))


def _flight(folder: Path, *, tagged: bool = True, gps_noise: float = 0.5,
            seed: int = 1) -> dict[str, complex]:
    """Photos of the made-up field in serpentine lines; returns each photo's true middle."""
    rng = np.random.default_rng(seed)
    field = _field(rng)
    per_px = 0.01
    to_field = Affine(per_px, 0, ORIGIN[0], 0, -per_px, ORIGIN[1] + field.shape[0] * per_px)
    to_wgs = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
    folder.mkdir(parents=True, exist_ok=True)
    truth, when, n = {}, datetime(2025, 5, 20, 9, 0), 0
    for line, x in enumerate(np.arange(8.0, 34.0, 5.0)):
        heading = 0.0 if line % 2 == 0 else 180.0
        ys = np.arange(8.0, 34.0, 2.5)
        for y in (ys if heading == 0 else ys[::-1]):
            centre = complex(ORIGIN[0] + x, ORIGIN[1] + y)
            yaw = heading + rng.normal(0, 1.5)
            z = GSD * complex(math.cos(math.radians(yaw)), -math.sin(math.radians(yaw)))
            to_ground = Affine(z.real, z.imag, centre.real - z.real * W / 2 - z.imag * H / 2,
                               z.imag, -z.real, centre.imag - z.imag * W / 2 + z.real * H / 2)
            m = ~to_field * to_ground
            photo = cv2.warpAffine(field, np.array([[m.a, m.b, m.c], [m.d, m.e, m.f]]), (W, H),
                                   flags=cv2.INTER_AREA | cv2.WARP_INVERSE_MAP)
            tag = centre + complex(*rng.normal(0, gps_noise, 2))
            lon, lat = to_wgs.transform(tag.real, tag.imag)
            path = folder / f"DJI_{n:04d}.JPG"
            ok, jpeg = cv2.imencode(".jpg", cv2.cvtColor(photo, cv2.COLOR_RGB2BGR))
            path.write_bytes(bytes(jpeg))
            zeroth = {piexif.ImageIFD.Model: b"FC6310"} if tagged else {}
            exif = {piexif.ExifIFD.DateTimeOriginal: when.strftime("%Y:%m:%d %H:%M:%S").encode()}
            if tagged:
                exif[piexif.ExifIFD.FocalLength] = (88, 10)
            piexif.insert(piexif.dump({"0th": zeroth, "Exif": exif, "GPS": {
                piexif.GPSIFD.GPSLatitudeRef: b"N", piexif.GPSIFD.GPSLatitude: _dms(lat),
                piexif.GPSIFD.GPSLongitudeRef: b"W", piexif.GPSIFD.GPSLongitude: _dms(lon)}}),
                str(path))
            if tagged:
                # The height a Phantom 4 Pro flies for 2 cm pixels at this photo width.
                height = GSD * 8.8 * W / 13.2
                xmp = (f'<rdf:Description drone-dji:RelativeAltitude="+{height:.2f}" '
                       f'drone-dji:GimbalYawDegree="{heading if heading <= 180 else heading - 360}"'
                       f'/>').encode()
                segment = b"http://ns.adobe.com/xap/1.0/\x00" + xmp
                data = path.read_bytes()
                path.write_bytes(data[:2] + b"\xff\xe1" + (len(segment) + 2).to_bytes(2, "big")
                                 + segment + data[2:])
            truth[path.name] = centre
            when += timedelta(seconds=2)
            n += 1
    return truth


@pytest.fixture(scope="module")
def tagged_flight(tmp_path_factory) -> tuple[Path, dict]:
    folder = tmp_path_factory.mktemp("tagged")
    return folder, _flight(folder)


def _errors(stitched, truth) -> np.ndarray:
    """How far each photo's placed middle is from its true one, after the common offset.

    The whole flight's GPS offset moves the map; that is the GPS, not the stitching.
    """
    to_utm = Transformer.from_crs("EPSG:4326", stitched.crs, always_xy=True)
    to_wgs = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
    placed, real = [], []
    for photo in stitched.photos:
        centre = truth[photo.path.stem + ".JPG"]
        lon, lat = to_wgs.transform(centre.real, centre.imag)
        east, north = to_utm.transform(lon, lat)
        placed.append(photo.t)
        real.append(complex(east, north))
    placed, real = np.array(placed), np.array(real)
    return np.abs((placed - real) - np.mean(placed - real))


def test_tagged_photos_are_put_back_where_they_were_taken(tagged_flight) -> None:
    folder, truth = tagged_flight
    stitched = photos.place(photos.find_photos(folder))
    assert stitched.connected == len(stitched.photos) == len(truth)
    assert stitched.tie_rms_m < 0.03
    assert np.median(_errors(stitched, truth)) < 0.10
    sizes = [photo.metres_per_px for photo in stitched.photos]
    assert np.median(sizes) == pytest.approx(GSD, rel=0.03)


def test_photos_with_only_gps_are_sized_and_turned_by_their_overlaps(tmp_path) -> None:
    """No height, camera or heading: all of it worked out from ties and GPS spacing."""
    truth = _flight(tmp_path, tagged=False)
    stitched = photos.place(photos.find_photos(tmp_path))
    assert any("did not say their height or heading" in note for note in stitched.all_notes)
    assert np.median(_errors(stitched, truth)) < 0.25
    assert np.median([p.metres_per_px for p in stitched.photos]) == pytest.approx(GSD, rel=0.06)


def test_the_painted_map_matches_the_field_it_was_cut_from(tagged_flight, tmp_path) -> None:
    folder, _ = tagged_flight
    stitched = photos.place(photos.find_photos(folder))
    out = photos.paint(stitched, tmp_path / "map.tif")
    with rasterio.open(out) as dataset:
        assert dataset.count == 4 and str(dataset.crs) == CRS
        assert abs(dataset.transform.a) == pytest.approx(GSD, rel=0.05)
        covered = dataset.read(4) > 0
        green_minus_red = dataset.read(2).astype(float) - dataset.read(1).astype(float)
        per_px = abs(dataset.transform.a)
    assert covered.mean() > 0.6
    # The rows are still there, at their true spacing: seams that had slipped
    # would smear them out of step across the map.
    h, w = covered.shape
    middle = np.s_[h // 4:3 * h // 4, w // 4:3 * w // 4]
    assert covered[middle].all()
    profile = green_minus_red[middle].mean(axis=0)
    spectrum = np.abs(np.fft.rfft(profile - profile.mean()))
    frequencies = np.fft.rfftfreq(len(profile), d=per_px)
    plausible = (frequencies > 1 / 3.0) & (frequencies < 1 / 0.3)
    strongest = frequencies[plausible][np.argmax(spectrum[plausible])]
    assert 1 / strongest == pytest.approx(0.76, rel=0.05)
    # ...and sharply so: a map whose seams slipped smears that peak out.
    assert spectrum[plausible].max() > 5 * np.median(spectrum[plausible])


def test_photos_without_gps_are_refused_plainly(tmp_path) -> None:
    for n in range(3):
        cv2.imwrite(str(tmp_path / f"IMG_{n}.jpg"), np.zeros((20, 20, 3), np.uint8))
    with pytest.raises(photos.StitchError, match="GPS position"):
        photos.place(photos.find_photos(tmp_path))


def test_a_tiff_s_gps_tag_is_read(tmp_path) -> None:
    """A TIFF reads its GPS block from the open file, so it must be asked while open."""
    path = tmp_path / "frame.tif"
    Image.fromarray(np.full((8, 8), 305.0, np.float32), mode="F").save(path, exif=piexif.dump({
        "0th": {}, "GPS": {piexif.GPSIFD.GPSLatitudeRef: b"N",
                           piexif.GPSIFD.GPSLatitude: _dms(26.2),
                           piexif.GPSIFD.GPSLongitudeRef: b"W",
                           piexif.GPSIFD.GPSLongitude: _dms(97.9)}}))
    shot = ingest.read_shot(path)
    assert shot.lat == pytest.approx(26.2, abs=1e-5) and shot.lon == pytest.approx(-97.9, abs=1e-5)


def test_thermal_photos_become_placed_frames_the_mosaic_reads(tmp_path) -> None:
    """One-band photos with GPS go out as north-up GeoTIFFs with their capture time."""
    colour = tmp_path / "colour"
    truth = _flight(colour)
    thermal = tmp_path / "thermal"
    thermal.mkdir()
    for path in sorted(colour.glob("*.JPG")):
        grey = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE).astype(np.float32)
        kelvin = 300.0 + grey / 20.0          # temperatures that follow the picture
        exif = Image.open(path).getexif()
        gps = dict(exif.get_ifd(0x8825))
        Image.fromarray(kelvin, mode="F").save(thermal / (path.stem + ".tif"), exif=piexif.dump({
            "0th": {}, "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2025:05:20 09:00:00"},
            "GPS": {1: gps[1].encode(), 2: tuple(_to_rational(v) for v in gps[2]),
                    3: gps[3].encode(), 4: tuple(_to_rational(v) for v in gps[4])}}))
    stitched = photos.place(photos.find_photos(thermal))
    assert np.median(_errors(stitched, truth)) < 0.25
    frames = photos.write_frames(stitched, tmp_path / "placed")
    assert len(frames) == len(truth)
    with rasterio.open(frames[0]) as dataset:
        assert dataset.transform.b == 0 and dataset.transform.d == 0     # north up
        assert dataset.tags()["TIFFTAG_DATETIME"] == "2025:05:20 09:00:00"
        assert np.nanmedian(dataset.read(1)) > 300
    assert mosaic.frame_time(frames[0], {"TIFFTAG_DATETIME": "2025:05:20 09:00:00"})


def _to_rational(value: float) -> tuple[int, int]:
    return (int(round(float(value) * 10000)), 10000)


def test_the_camera_s_own_pattern_comes_off_before_placing() -> None:
    """A bar of warm shade in the same spot in every frame goes; the field does not."""
    rng = np.random.default_rng(0)
    pattern = np.zeros((40, 60))
    pattern[:, 20:30] = 2.0                    # the camera's warm bar
    frames = [300 + rng.normal(0, 1, (40, 60)) + pattern for _ in range(30)]
    taken = [datetime(2025, 5, 20, 9, 0) + timedelta(seconds=5 * k) for k in range(30)]
    flat = photos.camera_pattern(frames, taken)
    columns = np.mean([frame.mean(axis=0) for frame in flat], axis=0)
    assert columns[20:30].mean() - columns[:20].mean() < 0.2
