"""Local sign-in settings; reading the app's Keychain item is user initiated."""
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)
from .credentials import CredentialError, CredentialStore


class SettingsDialog(QDialog):
    def __init__(self, parent=None, store=None):
        super().__init__(parent)
        self.store = store or CredentialStore()
        self.setWindowTitle('Settings · Gracenote sign-in')
        self.resize(560, 360)
        layout = QVBoxLayout(self)
        description = QLabel('Save your Gracenote sign-in details in this Mac’s Keychain.\n'
                             'They stay local and are never included in workbooks, logs or GitHub releases.')
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QFormLayout()
        self.username = QLineEdit()
        self.username.setAccessibleName('Gracenote username or email')
        self.username.setPlaceholderText('Username or email')
        self.password = QLineEdit()
        self.password.setAccessibleName('Gracenote password')
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow('Username / email', self.username)
        form.addRow('Password', self.password)
        layout.addLayout(form)
        show_password = QCheckBox('Show password')
        show_password.toggled.connect(lambda checked: self.password.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
        layout.addWidget(show_password)
        instructions = QLabel('During sign-in, click Fill saved sign-in in the app, or use the Copy buttons.\n'
                              'The app detects when you finish signing in and opens Programs automatically.')
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        copy_buttons = QHBoxLayout()
        self.copy_username = QPushButton('Copy username')
        self.copy_password = QPushButton('Copy password')
        self.copy_username.clicked.connect(lambda: QApplication.clipboard().setText(self.username.text()))
        self.copy_password.clicked.connect(lambda: QApplication.clipboard().setText(self.password.text()))
        copy_buttons.addWidget(self.copy_username)
        copy_buttons.addWidget(self.copy_password)
        layout.addLayout(copy_buttons)
        self.message = QLabel('No saved credentials on this Mac.')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.remove_button = QPushButton('Remove saved credentials')
        self.remove_button.clicked.connect(self.remove_saved)
        layout.addWidget(self.remove_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.username.textChanged.connect(self.update_buttons)
        self.password.textChanged.connect(self.update_buttons)
        try:
            saved = self.store.load()
            if saved:
                self.username.setText(saved['username'])
                self.password.setText(saved['password'])
                self.message.setText('Saved locally in macOS Keychain.')
            self.remove_button.setEnabled(bool(saved))
        except CredentialError as exc:
            self.message.setText(str(exc))
            self.remove_button.setEnabled(False)
        self.update_buttons()

    def update_buttons(self):
        self.copy_username.setEnabled(bool(self.username.text()))
        self.copy_password.setEnabled(bool(self.password.text()))

    def save(self):
        try:
            self.store.save(self.username.text(), self.password.text())
        except CredentialError as exc:
            QMessageBox.warning(self, 'Could not save credentials', str(exc))
            return
        self.accept()

    def remove_saved(self):
        try:
            self.store.remove()
        except CredentialError as exc:
            QMessageBox.warning(self, 'Could not remove credentials', str(exc))
            return
        self.username.clear()
        self.password.clear()
        self.remove_button.setEnabled(False)
        self.message.setText('Saved credentials removed from this Mac.')
