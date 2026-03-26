from pathlib import Path

from cdmm.archive.transactional_io import TransactionalIO


def test_stage_and_commit(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "file1.txt").write_text("original1")
    (game_dir / "file2.txt").write_text("original2")

    staging = tmp_path / "staging"
    staging.mkdir()

    txn = TransactionalIO(game_dir, staging)
    txn.stage_file("file1.txt", b"modified1")
    txn.stage_file("file2.txt", b"modified2")
    txn.commit()

    assert (game_dir / "file1.txt").read_bytes() == b"modified1"
    assert (game_dir / "file2.txt").read_bytes() == b"modified2"
    # No .pre-apply files remaining
    assert list(game_dir.glob("*.pre-apply")) == []


def test_stage_subdirectory(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    (game_dir / "0008").mkdir(parents=True)
    (game_dir / "0008" / "0.paz").write_bytes(b"original_paz")

    staging = tmp_path / "staging"
    staging.mkdir()

    txn = TransactionalIO(game_dir, staging)
    txn.stage_file("0008/0.paz", b"modified_paz")
    txn.commit()

    assert (game_dir / "0008" / "0.paz").read_bytes() == b"modified_paz"


def test_commit_new_file(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()

    staging = tmp_path / "staging"
    staging.mkdir()

    txn = TransactionalIO(game_dir, staging)
    txn.stage_file("new_file.txt", b"new content")
    txn.commit()

    assert (game_dir / "new_file.txt").read_bytes() == b"new content"


def test_detect_interrupted_apply(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "file.txt.pre-apply").write_text("backup")

    pre_apply = TransactionalIO.detect_interrupted_apply(game_dir)
    assert len(pre_apply) == 1


def test_recover_from_interrupted(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "file.txt").write_text("corrupted")
    (game_dir / "file.txt.pre-apply").write_text("original")

    count = TransactionalIO.recover_from_interrupted(game_dir)
    assert count == 1
    assert (game_dir / "file.txt").read_text() == "original"
    assert not (game_dir / "file.txt.pre-apply").exists()


def test_no_interrupted_apply(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()

    pre_apply = TransactionalIO.detect_interrupted_apply(game_dir)
    assert len(pre_apply) == 0
