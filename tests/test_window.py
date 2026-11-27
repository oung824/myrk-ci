import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event

import pytest
from pydantic_ai.models import Model
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from myrk.conversation import Conversation
from myrk.window import MainWindow


@pytest.fixture(scope="module")
def application() -> Iterator[QApplication]:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    assert app.platformName() == "offscreen"
    yield app


def wait_until(condition: Callable[[], bool]) -> None:
    for _ in range(500):
        QApplication.processEvents()
        if condition():
            return
        QTest.qWait(10)
    raise AssertionError("窗口没有在五秒内完成预期动作。")


def test_window_sends_reopens_and_continues(
    application: QApplication, tmp_path: Path
) -> None:
    @asynccontextmanager
    async def model_factory(_key: str) -> AsyncIterator[Model]:
        yield TestModel(call_tools=["read_authorized_text"], custom_output_text="规则已理解。")

    document = tmp_path / "规则.md"
    document.write_text("采购需审批。", encoding="utf-8")
    window = MainWindow(data_directory=tmp_path / "records", model_factory=model_factory)
    window.show()
    assert not (tmp_path / "records").exists()
    window.key_input.setText("本机检查用，不是真实密钥")
    window.document_input.setText(str(document))
    window.prompt_input.setPlainText("请读取规则。")
    QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: window.send_button.isEnabled())
    assert "规则已理解。" in window.transcript.toPlainText()
    path = window.conversation.path
    window.close()

    reopened = MainWindow(data_directory=tmp_path / "records", model_factory=model_factory)
    reopened.show()

    def select_record_in_dialog() -> None:
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QFileDialog)
        dialog.selectFile(str(path))
        dialog.accept()

    QTimer.singleShot(0, select_record_in_dialog)
    QTest.mouseClick(reopened.open_button, Qt.MouseButton.LeftButton)
    assert "请读取规则。" in reopened.transcript.toPlainText()
    reopened.key_input.setText("本机检查用，不是真实密钥")
    reopened.document_input.setText(str(document))
    reopened.prompt_input.setPlainText("继续说明。")
    QTest.mouseClick(reopened.send_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: reopened.send_button.isEnabled())
    assert [turn.prompt for turn in Conversation.open(path).turns] == [
        "请读取规则。",
        "继续说明。",
    ]
    reopened.close()


def test_stop_and_close_wait_for_model_cleanup_without_freezing_window(
    application: QApplication, tmp_path: Path
) -> None:
    connection_closed = Event()

    async def stream(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str]:
        yield "这段已经到达"
        await asyncio.sleep(30)
        yield "不应到达"

    @asynccontextmanager
    async def model_factory(_key: str) -> AsyncIterator[Model]:
        try:
            yield FunctionModel(stream_function=stream)
        finally:
            await asyncio.sleep(0.15)
            connection_closed.set()

    document = tmp_path / "规则.txt"
    document.write_text("授权内容", encoding="utf-8")
    window = MainWindow(data_directory=tmp_path / "records", model_factory=model_factory)
    window.show()
    window.key_input.setText("本机检查用")
    window.document_input.setText(str(document))
    window.prompt_input.setPlainText("开始说明。")
    QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: "这段已经到达" in window.transcript.toPlainText())
    QTest.mouseClick(window.stop_button, Qt.MouseButton.LeftButton)
    assert "正在停止" in window.status_label.text()
    assert not window.send_button.isEnabled()
    responsive: list[bool] = []
    QTimer.singleShot(0, lambda: responsive.append(True))
    wait_until(lambda: bool(responsive))
    wait_until(lambda: window.send_button.isEnabled())
    assert connection_closed.is_set()
    assert "已停止" in window.status_label.text()
    assert "未知" in window.status_label.text()
    assert "送出：未知" in window.transcript.toPlainText()
    assert "不应到达" not in window.transcript.toPlainText()
    assert Conversation.open(window.conversation.path).turns[-1].status == "cancelled"

    connection_closed.clear()
    window.prompt_input.setPlainText("再次开始，随后关闭。")
    QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: window.transcript.toPlainText().count("这段已经到达") == 2)
    window.close()
    assert window.isVisible()
    assert "正在停止" in window.status_label.text()
    wait_until(lambda: not window.isVisible())
    assert connection_closed.is_set()
    assert len(Conversation.open(window.conversation.path).turns) == 2


def test_corrupt_record_is_rejected_without_changing_original_file(
    application: QApplication, tmp_path: Path
) -> None:
    damaged = tmp_path / "损坏记录.json"
    damaged.write_text('{"version":1,"turns":[这不是有效记录', encoding="utf-8")
    before = damaged.read_bytes()
    window = MainWindow(data_directory=tmp_path / "records")
    previous_path = window.conversation.path
    window.show()

    assert not window.open_conversation(damaged)
    assert "记录已损坏" in window.status_label.text()
    assert window.conversation.path == previous_path
    assert damaged.read_bytes() == before
    assert not (tmp_path / "records").exists()
    window.close()


def test_missing_connection_and_unsupported_document_are_explained_before_sending(
    application: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(data_directory=tmp_path / "records")
    window.show()
    QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
    assert "密钥" in window.status_label.text()
    window.key_input.setText("本机检查用")
    QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
    assert "输入这次要做的事" in window.status_label.text()
    window.prompt_input.setPlainText("读取这份资料。")
    QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
    assert "存在的授权文本" in window.status_label.text()
    unsupported = tmp_path / "图像.pdf"
    unsupported.write_bytes(b"not-a-text-document")
    window.document_input.setText(str(unsupported))
    QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
    assert "只支持" in window.status_label.text()
    assert not (tmp_path / "records").exists()
    window.close()


def test_old_chinese_text_format_explains_failure_and_preserves_question_and_file(
    application: QApplication, tmp_path: Path
) -> None:
    model_requested = Event()

    async def stream(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str]:
        model_requested.set()
        yield "不应收到回答"

    @asynccontextmanager
    async def model_factory(_key: str) -> AsyncIterator[Model]:
        yield FunctionModel(stream_function=stream)

    document = tmp_path / "旧格式规则.txt"
    original = "采购需审批。".encode("gbk")
    document.write_bytes(original)
    window = MainWindow(data_directory=tmp_path / "records", model_factory=model_factory)
    window.show()
    window.key_input.setText("本机检查用")
    window.document_input.setText(str(document))
    window.prompt_input.setPlainText("请读取这份旧格式资料。")
    QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: window.send_button.isEnabled())

    assert "UTF-8（常见文字保存格式）" in window.status_label.text()
    assert "请读取这份旧格式资料。" in window.transcript.toPlainText()
    assert document.read_bytes() == original
    assert not model_requested.is_set()
    window.close()
