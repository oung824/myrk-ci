import asyncio
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pydantic_ai import CancellationToken
from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtGui import QCloseEvent, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from myrk.conversation import Conversation, Turn
from myrk.provider import ModelFactory, deepseek_model


class ConversationWorker(QThread):
    update = Signal(str, str)

    def __init__(
        self,
        conversation: Conversation,
        prompt: str,
        document: Path,
        key: str,
        model_factory: ModelFactory,
        thinking: bool,
        request_limit: int,
    ) -> None:
        super().__init__()
        self.conversation = conversation
        self.prompt = prompt
        self.document = document
        self.key = key
        self.model_factory = model_factory
        self.thinking = thinking
        self.request_limit = request_limit
        self.token = CancellationToken()
        self.turn: Turn | None = None
        self.failed = False

    def run(self) -> None:
        try:
            asyncio.run(self._ask())
        except Exception:
            self.failed = True
        finally:
            self.key = ""

    async def _ask(self) -> None:
        async with self.model_factory(self.key) as model:
            self.turn = await self.conversation.ask(
                self.prompt,
                document=self.document,
                model=model,
                on_update=lambda update: self.update.emit(update.kind, update.text),
                cancellation_token=self.token,
                thinking=self.thinking,
                request_limit=self.request_limit,
            )


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        data_directory: Path | None = None,
        model_factory: ModelFactory = deepseek_model,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Myrk")
        self.resize(920, 760)
        self.data_directory = data_directory or Path(__file__).resolve().parents[1] / ".myrk"
        self.model_factory = model_factory
        self.worker: ConversationWorker | None = None
        self._close_pending = False

        body = QWidget()
        layout = QVBoxLayout(body)
        self.new_button = QPushButton("新会话")
        self.open_button = QPushButton("打开已有记录…")
        session_actions = QHBoxLayout()
        session_actions.addWidget(self.new_button)
        session_actions.addWidget(self.open_button)
        session_actions.addStretch()
        layout.addLayout(session_actions)
        self.record_label = QLabel()
        self.record_label.setWordWrap(True)
        layout.addWidget(self.record_label)

        form = QFormLayout()
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("仅本次运行使用；不会保存到记录")
        form.addRow("DeepSeek（模型服务）账户密钥（连接用密码）：", self.key_input)
        document_row = QHBoxLayout()
        self.document_input = QLineEdit()
        self.document_input.setPlaceholderText("选择普通文本文件或用简单符号排版的说明文件")
        self.document_button = QPushButton("选择文件…")
        document_row.addWidget(self.document_input)
        document_row.addWidget(self.document_button)
        form.addRow("本次授权资料：", document_row)
        layout.addLayout(form)
        notice = QLabel(
            "发送即表示允许读取上方这一份文本，并将其内容提供给本次联网请求。"
            "请先确认文件位置和内容；不会读取其他文件。"
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        options = QHBoxLayout()
        options.addWidget(QLabel("deepseek-flash（本次使用的模型调用名）"))
        self.thinking_input = QCheckBox("额外思考（可能增加等待和用量）")
        options.addWidget(self.thinking_input)
        options.addWidget(QLabel("每次最多处理轮数："))
        self.request_limit_input = QSpinBox()
        self.request_limit_input.setRange(1, 100)
        self.request_limit_input.setValue(10)
        options.addWidget(self.request_limit_input)
        layout.addLayout(options)
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        layout.addWidget(self.transcript, 1)
        self.prompt_input = QPlainTextEdit()
        self.prompt_input.setPlaceholderText("输入这次要做的事；本段暂不支持运行途中追加指令")
        self.prompt_input.setMaximumHeight(110)
        layout.addWidget(self.prompt_input)
        self.status_label = QLabel("准备就绪。")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        actions = QHBoxLayout()
        self.send_button = QPushButton("发送")
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        actions.addStretch()
        actions.addWidget(self.send_button)
        actions.addWidget(self.stop_button)
        layout.addLayout(actions)
        self.setCentralWidget(body)

        self.new_button.clicked.connect(self.new_conversation)
        self.open_button.clicked.connect(self._choose_record)
        self.document_button.clicked.connect(self._choose_document)
        self.send_button.clicked.connect(self._send)
        self.stop_button.clicked.connect(self._stop)
        self.new_conversation()

    @Slot()
    def new_conversation(self) -> None:
        if self.worker is not None:
            return
        name = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.json"
        self.conversation = Conversation(self.data_directory / name)
        self.record_label.setText(f"本次记录：{self.conversation.path}")
        self.transcript.clear()
        self.prompt_input.clear()
        self.status_label.setText("新会话已准备；发送后才会创建记录。")

    def open_conversation(self, path: Path) -> bool:
        if self.worker is not None:
            return False
        try:
            conversation = Conversation.open(path)
        except (ValueError, OSError):
            self.status_label.setText("无法打开：记录已损坏、格式不受支持，或文件无法读取。原文件未改动。")
            return False
        self.conversation = conversation
        self.record_label.setText(f"本次记录：{path}")
        self._render_history()
        self.status_label.setText("已打开记录；选择本次授权资料后可以继续。")
        return True

    @Slot()
    def _choose_record(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "打开会话记录", str(self.data_directory), "会话记录（保存对话的文件，.json） (*.json)"
        )
        if path:
            self.open_conversation(Path(path))

    @Slot()
    def _choose_document(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择本次授权文本", "", "普通文本与说明文件 (*.txt *.md)"
        )
        if path:
            self.document_input.setText(path)

    @Slot()
    def _send(self) -> None:
        key = self.key_input.text().strip()
        prompt = self.prompt_input.toPlainText().strip()
        document = Path(self.document_input.text().strip())
        if not key:
            self.status_label.setText("请先填写本次使用的账户密钥。")
            return
        if not prompt:
            self.status_label.setText("请先输入这次要做的事。")
            return
        if not self.document_input.text().strip() or not document.is_file():
            self.status_label.setText("请选择一份存在的授权文本文件。")
            return
        if document.suffix.lower() not in {".txt", ".md"}:
            self.status_label.setText("本段只支持 .txt（普通文本）和 .md（用简单符号排版的文本）文件。")
            return

        self.worker = ConversationWorker(
            self.conversation,
            prompt,
            document,
            key,
            self.model_factory,
            self.thinking_input.isChecked(),
            self.request_limit_input.value(),
        )
        self.worker.update.connect(self._receive_update)
        self.worker.finished.connect(self._finished)
        self._set_running(True)
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)
        self.transcript.insertPlainText(
            ("\n\n" if self.transcript.toPlainText() else "") + f"你：{prompt}\n\nMyrk："
        )
        self.status_label.setText("正在连接并等待回答…")
        self.worker.start()

    @Slot(str, str)
    def _receive_update(self, kind: str, text: str) -> None:
        if kind == "text":
            self.transcript.moveCursor(QTextCursor.MoveOperation.End)
            self.transcript.insertPlainText(text)
            self.transcript.ensureCursorVisible()
        elif self.worker is not None and not self.worker.token.cancelled:
            self.status_label.setText(text)

    @Slot()
    def _stop(self) -> None:
        if self.worker is not None:
            self.worker.token.cancel()
            self.stop_button.setEnabled(False)
            self.status_label.setText("正在停止；等待本次请求实际退出…")

    @Slot()
    def _finished(self) -> None:
        assert self.worker is not None
        worker = self.worker
        self.worker = None
        self._render_history()
        if worker.failed or worker.turn is None:
            self.status_label.setText("本轮未完成。请检查网络、账户设置及记录文件是否可写，再重试。")
        else:
            messages = {
                "completed": "已完成，记录已保存。",
                "cancelled": "已停止，已收到的内容已保存；本次完整用量未知。",
                "failed": "本轮失败，已收到的内容已保存。请检查连接和账户设置。",
                "limited": "已达到本次处理轮数上限，已有内容已保存。",
                "running": "本轮未完整结束。",
            }
            if worker.turn.status in {"failed", "limited"} and worker.turn.error:
                self.status_label.setText(worker.turn.error)
            else:
                self.status_label.setText(messages[worker.turn.status])
        self._set_running(False)
        worker.deleteLater()
        if self._close_pending:
            self.close()

    def _set_running(self, running: bool) -> None:
        for widget in (
            self.send_button,
            self.new_button,
            self.open_button,
            self.document_button,
            self.document_input,
            self.key_input,
            self.prompt_input,
            self.thinking_input,
            self.request_limit_input,
        ):
            widget.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def _render_history(self) -> None:
        labels = {
            "completed": "已完成",
            "cancelled": "已停止",
            "failed": "失败",
            "limited": "达到处理轮数上限",
            "running": "未完整结束",
        }
        self.transcript.setPlainText(
            "\n\n".join(
                f"你：{turn.prompt}\n\nMyrk：{turn.answer}\n\n状态：{labels[turn.status]}"
                f"；模型已处理 {turn.requests} 轮。\n"
                "本次用量（服务计费时计算的小段文字数）："
                f"送出：{turn.input_tokens if turn.input_tokens is not None else '未知'}；"
                f"收到：{turn.output_tokens if turn.output_tokens is not None else '未知'}。"
                for turn in self.conversation.turns
            )
        )
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker is not None:
            self._close_pending = True
            self._stop()
            event.ignore()
        else:
            event.accept()
