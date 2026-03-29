"""Windows-focused tests for environments/tool_context.py file transfers."""

from unittest.mock import patch

from environments.tool_context import ToolContext


class TestToolContextWindowsTransfers:
    def test_upload_file_writes_directly_for_windows_local_backend(self, tmp_path):
        source = tmp_path / "source.bin"
        source.write_bytes(b"\x00\x01hello")
        target = tmp_path / "nested" / "target.bin"

        with patch("environments.tool_context._is_windows_local_backend", return_value=True):
            result = ToolContext("task-1").upload_file(str(source), str(target))

        assert result["exit_code"] == 0
        assert target.read_bytes() == b"\x00\x01hello"

    def test_download_file_reads_directly_for_windows_local_backend(self, tmp_path):
        remote = tmp_path / "remote.bin"
        remote.write_bytes(b"binary-data")
        local = tmp_path / "downloads" / "copy.bin"

        with patch("environments.tool_context._is_windows_local_backend", return_value=True):
            result = ToolContext("task-1").download_file(str(remote), str(local))

        assert result == {"success": True, "bytes": len(b"binary-data")}
        assert local.read_bytes() == b"binary-data"

    def test_download_file_reports_missing_remote_file(self, tmp_path):
        missing = tmp_path / "missing.bin"
        local = tmp_path / "downloads" / "copy.bin"

        with patch("environments.tool_context._is_windows_local_backend", return_value=True):
            result = ToolContext("task-1").download_file(str(missing), str(local))

        assert result["success"] is False
        assert "Remote file not found" in result["error"]
