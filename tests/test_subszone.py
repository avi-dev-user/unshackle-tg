import hashlib

from src import subszone


def test_subtitle_extension_detection():
    assert subszone.is_subtitle('/tmp/movie.he.srt')
    assert subszone.is_subtitle('/tmp/movie.VTT')
    assert not subszone.is_subtitle('/tmp/movie.mkv')


def test_idempotency_key_is_content_based(tmp_path):
    first = tmp_path / 'one.srt'
    second = tmp_path / 'renamed.srt'
    first.write_bytes(b'1\n00:00:00,000 --> 00:00:01,000\ntext\n')
    second.write_bytes(first.read_bytes())
    expected = 'unshackle:' + hashlib.sha256(first.read_bytes()).hexdigest()
    assert subszone.idempotency_key(str(first)) == expected
    assert subszone.idempotency_key(str(second)) == expected
