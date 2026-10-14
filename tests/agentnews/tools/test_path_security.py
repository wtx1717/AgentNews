import pytest

from agentnews.tools.path_security import (
    FileTooLargeError,
    InvalidTextFileError,
    ManagedPathResolver,
    PathTypeError,
    SensitivePathError,
    UnsafePathError,
)


def test_resolves_root_relative_and_absolute_paths(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    note = root / "note.txt"
    note.write_text("hello", encoding="utf-8")
    resolver = ManagedPathResolver(root)

    assert resolver.resolve("note.txt", expected_type="file") == note.resolve()
    assert resolver.resolve(note, expected_type="file") == note.resolve()
    assert resolver.resolve(".", expected_type="directory") == root.resolve()


@pytest.mark.parametrize(
    "path",
    [
        "",
        "../outside.txt",
        "notes/../../outside.txt",
        r"notes\..\outside.txt",
        r"C:\outside\note.txt",
        r"C:relative.txt",
        "/outside/note.txt",
        "note\x00.txt",
    ],
)
def test_rejects_empty_traversal_and_outside_paths(tmp_path, path):
    root = tmp_path / "data"
    root.mkdir()
    resolver = ManagedPathResolver(root)

    with pytest.raises(UnsafePathError):
        resolver.resolve(path)


def test_rejects_file_and_directory_type_mismatches(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "note.txt").write_text("hello", encoding="utf-8")
    (root / "folder").mkdir()
    resolver = ManagedPathResolver(root)

    with pytest.raises(PathTypeError):
        resolver.resolve("note.txt", expected_type="directory")
    with pytest.raises(PathTypeError):
        resolver.resolve("folder", expected_type="file")


def test_allows_only_a_missing_leaf_with_an_existing_parent(tmp_path):
    root = tmp_path / "data"
    (root / "notes").mkdir(parents=True)
    resolver = ManagedPathResolver(root)

    assert resolver.resolve(
        "notes/new.txt", expected_type="file", allow_missing_leaf=True
    ) == (root / "notes" / "new.txt").resolve()
    with pytest.raises(PathTypeError, match="parent directory"):
        resolver.resolve(
            "missing/new.txt", expected_type="file", allow_missing_leaf=True
        )
    with pytest.raises(PathTypeError):
        resolver.resolve("missing", allow_missing_leaf=True)


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env",
        ".env.local",
        "keys/id_rsa",
        "keys/server.pem",
        "keys/server.key",
        "keys/identity.p12",
        "keys/identity.pfx",
        "keys/trust.jks",
        "keys/trust.keystore",
        "agentnews.db",
        "agentnews.sqlite",
        "agentnews.sqlite3",
        "agentnews.sqlite3-wal",
        "agentnews.sqlite3-shm",
        "agentnews.sqlite3-journal",
    ],
)
def test_rejects_sensitive_file_names_even_for_new_targets(tmp_path, relative_path):
    root = tmp_path / "data"
    (root / "keys").mkdir(parents=True)
    resolver = ManagedPathResolver(root)

    with pytest.raises(SensitivePathError):
        resolver.resolve(
            relative_path, expected_type="file", allow_missing_leaf=True
        )


def test_rejects_symlink_escape_and_allows_in_root_symlink_when_supported(tmp_path):
    root = tmp_path / "data"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = root / "target.txt"
    target.write_text("inside", encoding="utf-8")
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    resolver = ManagedPathResolver(root)

    try:
        (root / "inside-link.txt").symlink_to(target)
        (root / "outside-link.txt").symlink_to(outside / "secret.txt")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"file symlinks are unavailable: {error}")

    assert resolver.resolve("inside-link.txt", expected_type="file") == target.resolve()
    with pytest.raises(UnsafePathError, match="escapes"):
        resolver.resolve("outside-link.txt", expected_type="file")


def test_rejects_dangling_symlink_as_missing_write_target(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    resolver = ManagedPathResolver(root)
    try:
        (root / "dangling.txt").symlink_to(root / "missing.txt")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"file symlinks are unavailable: {error}")

    with pytest.raises(UnsafePathError, match="dangling"):
        resolver.resolve(
            "dangling.txt", expected_type="file", allow_missing_leaf=True
        )


def test_validates_file_size_before_reading(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "small.txt").write_bytes(b"1234")
    (root / "large.txt").write_bytes(b"12345")
    resolver = ManagedPathResolver(root, max_file_size_bytes=4)

    assert resolver.read_text("small.txt") == "1234"
    with pytest.raises(FileTooLargeError):
        resolver.resolve("large.txt", expected_type="file")
    with pytest.raises(FileTooLargeError):
        resolver.read_text("large.txt")


@pytest.mark.parametrize("content", [b"\xff", b"a\x00b"])
def test_rejects_non_utf8_or_nul_text_files(tmp_path, content):
    root = tmp_path / "data"
    root.mkdir()
    (root / "invalid.txt").write_bytes(content)
    resolver = ManagedPathResolver(root)

    with pytest.raises(InvalidTextFileError):
        resolver.read_text("invalid.txt")


def test_encodes_only_bounded_utf8_text(tmp_path):
    resolver = ManagedPathResolver(tmp_path / "data", max_file_size_bytes=4)

    assert resolver.encode_text("éé") == "éé".encode("utf-8")
    with pytest.raises(FileTooLargeError):
        resolver.encode_text("ééx")
    with pytest.raises(InvalidTextFileError):
        resolver.encode_text("a\x00b")
    with pytest.raises(InvalidTextFileError):
        resolver.encode_text("\ud800")


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_requires_positive_integer_file_size_limit(tmp_path, limit):
    with pytest.raises(ValueError, match="positive integer"):
        ManagedPathResolver(tmp_path / "data", max_file_size_bytes=limit)
