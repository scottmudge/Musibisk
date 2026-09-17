#!/usr/bin/env python3
"""
Musibisk - A sleek, minimal music player with directory monitoring
"""

import sys
import json
import os
import re
import queue
from pathlib import Path
from typing import List, Optional
from enum import Enum
import base64
import time
from datetime import datetime
from urllib.parse import urlsplit

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSlider, QFileDialog, QMenuBar, QMenu,
    QListWidget, QListWidgetItem, QDialog, QFormLayout, QSpinBox,
    QDialogButtonBox, QFrame, QDial, QTableWidget, QTableWidgetItem,
    QHeaderView, QComboBox, QStyledItemDelegate, QStyleOptionViewItem,
    QStyle, QCheckBox, QLineEdit, QToolButton
)
from PyQt6.QtCore import (
    Qt, QTimer, QUrl, QThread, pyqtSignal, QObject, QByteArray,
    QModelIndex, QRect
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtGui import (
    QAction, QKeySequence, QIcon, QPixmap, QMouseEvent, QFont, QFontDatabase,
    QBrush, QColor, QPainter, QPainterPath
)
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import mutagen
try:
    import paramiko
except ImportError:
    paramiko = None
try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None
from resources import ICON_PNG_BASE64, BITMAP_FONT

# Hard-coded key used to obfuscate secrets at rest in config.json. This is NOT
# a security boundary (the key ships with the app); it just keeps values like
# the sync password from sitting in the config file as plain text.
_CONFIG_SECRET_KEY = b"TdkrPmF6emqbeZzi5CcZr5_6Hrm2m76A6Sck7rSSmfM="


def encrypt_secret(value: str) -> str:
    """Obfuscate a secret for storage in the config file.

    Returns the value unchanged when empty or when no cipher is available.
    """
    if not value or Fernet is None:
        return value
    return Fernet(_CONFIG_SECRET_KEY).encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    """Reverse encrypt_secret. Falls back to the raw value on any error, so
    legacy plain-text config values keep working (and get re-encrypted on
    the next save)."""
    if not value or Fernet is None:
        return value
    try:
        return Fernet(_CONFIG_SECRET_KEY).decrypt(value.encode()).decode()
    except Exception:
        return value

def read_track_info(filepath) -> dict:
    """Read display metadata for a track. Missing fields are None.

    Returns a dict with keys: title, artist, album, year, genre, track
    and cover ((bytes, mime) or None).
    """
    info = {'title': None, 'artist': None, 'album': None, 'year': None,
            'genre': None, 'track': None, 'cover': None}
    try:
        audio = mutagen.File(filepath)
        if audio is None:
            return info
        tags = getattr(audio, 'tags', None)
        if tags is None:
            return info
        if isinstance(audio, mutagen.mp3.MP3):
            def tx(key):
                frame = tags.get(key)
                if frame is not None and getattr(frame, 'text', None):
                    return str(frame.text[0])
                return None
            info['title'] = tx('TIT2')
            info['artist'] = tx('TPE1')
            info['album'] = tx('TALB')
            date = tx('TDRC') or tx('TYER')
            if date:
                info['year'] = date[:4]
            info['genre'] = tx('TCON')
            info['track'] = tx('TRCK')
            # APIC keys are exact-match too ('APIC' or 'APIC:<desc>')
            apic = None
            for key in tags.keys():
                if key.startswith('APIC'):
                    apic = tags[key]
                    break
            if apic is not None:
                info['cover'] = (bytes(apic.data), apic.mime or 'image/jpeg')
        elif isinstance(audio, mutagen.mp4.MP4):
            def tx4(key):
                vals = tags.get(key)
                if not vals:
                    return None
                v = vals[0]
                if isinstance(v, (tuple, list)):
                    v = v[0]
                return str(v)
            info['title'] = tx4('©nam')
            info['artist'] = tx4('©ART')
            info['album'] = tx4('©alb')
            info['year'] = tx4('©day')
            info['genre'] = tx4('©gen')
            info['track'] = tx4('trkn')
            covr = tags.get('covr')
            if covr:
                info['cover'] = (bytes(covr[0]), 'image/jpeg')
        else:
            def txv(*keys):
                for k in keys:
                    vals = tags.get(k)
                    if vals:
                        return str(vals[0])
                return None
            info['title'] = txv('TITLE', 'title')
            info['artist'] = txv('ARTIST', 'artist', 'ALBUMARTIST', 'albumartist')
            info['album'] = txv('ALBUM', 'album')
            date = txv('DATE', 'date', 'YEAR', 'year')
            if date:
                info['year'] = date[:4]
            info['genre'] = txv('GENRE', 'genre')
            info['track'] = txv('TRACKNUMBER', 'tracknumber',
                                'TRACK_NUMBER', 'track_number')
            pictures = tags.get('pictures')
            if pictures:
                info['cover'] = (bytes(pictures[0].data), 'image/png')
    except Exception:
        pass
    return info


# 'Saved' marker, stored as an embedded metadata tag instead of a filename
# prefix. A dedicated user-defined key (rather than POPM) is used so the
# marker cannot collide with rating/play-count data that other apps write:
#   MP3      -> ID3 TXXX frame  TXXX:MUSIBISK_SAVED = '1'
#   FLAC/OGG -> Vorbis Comment  MUSIBISK_SAVED = '1'
#   MP4/M4A  -> freeform atom   ----:com.musibisk:saved = '1'
SAVED_TAG_KEY = 'MUSIBISK_SAVED'
SAVED_TAG_VALUE = '1'
_M4A_SAVED_KEY = '----:com.musibisk:saved'


def read_saved_tag(filepath) -> bool:
    """Check if a file is marked as saved via its embedded metadata tag."""
    try:
        audio = mutagen.File(filepath)
        if audio is None:
            return False
        tags = getattr(audio, 'tags', None)
        if tags is None:
            return False
        if isinstance(audio, mutagen.mp3.MP3):
            # ID3 keys are exact-match ('TXXX:MUSIBISK_SAVED'); scan for it
            for key, frame in tags.items():
                if key.startswith('TXXX:') and key[5:].upper() == SAVED_TAG_KEY:
                    try:
                        return str(frame.text[0]) == SAVED_TAG_VALUE
                    except (ValueError, IndexError):
                        return False
            return False
        if isinstance(audio, mutagen.mp4.MP4):
            vals = tags.get(_M4A_SAVED_KEY)
            if not vals:
                return False
            return bytes(vals[0]) == SAVED_TAG_VALUE.encode()
        vals = tags.get(SAVED_TAG_KEY)  # Vorbis Comments are case-insensitive
        if not vals:
            return False
        return str(vals[0]) == SAVED_TAG_VALUE
    except Exception:
        return False


def set_saved_tag(filepath, saved: bool) -> bool:
    """Mark a file as saved (or remove the mark) in its metadata.

    Returns True on success.
    """
    try:
        audio = mutagen.File(filepath)
        if audio is None:
            return False
        if getattr(audio, 'tags', None) is None:
            try:
                audio.add_tags()
            except Exception:
                return False
        tags = audio.tags
        if tags is None:
            return False
        if isinstance(audio, mutagen.mp3.MP3):
            from mutagen.id3 import TXXX
            key = None
            for k in tags.keys():
                if k.startswith('TXXX:') and k[5:].upper() == SAVED_TAG_KEY:
                    key = k
                    break
            if saved:
                if key is not None:
                    tags[key].text = [SAVED_TAG_VALUE]
                else:
                    tags.add(TXXX(encoding=3, desc=SAVED_TAG_KEY,
                                  text=[SAVED_TAG_VALUE]))
            elif key is not None:
                del tags[key]
        elif isinstance(audio, mutagen.mp4.MP4):
            if saved:
                tags[_M4A_SAVED_KEY] = [SAVED_TAG_VALUE.encode()]
            else:
                try:
                    del tags[_M4A_SAVED_KEY]
                except KeyError:
                    pass
        else:
            # FLAC / OGG / OPUS (Vorbis Comments)
            if saved:
                tags[SAVED_TAG_KEY] = [SAVED_TAG_VALUE]
            else:
                try:
                    del tags[SAVED_TAG_KEY]
                except KeyError:
                    pass
        audio.save()
        return True
    except Exception as e:
        print(f"Error setting saved tag: {e}")
        return False


class LoopMode(Enum):
    NO_LOOP = 0
    LOOP_PLAYLIST = 1
    LOOP_SINGLE = 2

class PlayOrder(Enum):
    OLDEST_TO_NEWEST = 0
    NEWEST_TO_OLDEST = 1
    
BUTTON_FONT_SIZE = "font-size: 20px;"
    
    
def icon_from_base64_png(b64: str) -> QIcon:
    raw = base64.b64decode(b64)
    ba = QByteArray(raw)

    pixmap = QPixmap()
    pixmap.loadFromData(ba, "PNG")

    return QIcon(pixmap)

BitmapFontFamily = None

class ClickableSlider(QSlider):
    """Custom slider that allows clicking anywhere to seek"""
    
    def mousePressEvent(self, event: QMouseEvent):
        """Handle mouse press to seek to clicked position"""
        if event.button() == Qt.MouseButton.LeftButton:
            # Calculate the position based on where the user clicked
            value = QSlider.minimum(self) + ((QSlider.maximum(self) - QSlider.minimum(self)) * event.position().x()) / self.width()
            self.setValue(int(value))
            self.sliderMoved.emit(int(value))
            event.accept()
        else:
            super().mousePressEvent(event)


class FileWatcherHandler(FileSystemEventHandler):
    """Handles file system events for audio files"""
    
    AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.opus', '.aac', '.wma'}
    
    def __init__(self, directory: str, on_created, on_deleted):
        # NOTE: these must not be named on_created/on_deleted — watchdog
        # dispatches to the handler METHODS with those names.
        super().__init__()
        self.directory = Path(directory)
        self._on_created = on_created
        self._on_deleted = on_deleted
    
    def _is_audio(self, path) -> bool:
        return Path(path).suffix.lower() in self.AUDIO_EXTENSIONS
    
    def on_created(self, event):
        if not event.is_directory and self._is_audio(event.src_path):
            self._on_created(event.src_path)
    
    def on_deleted(self, event):
        if not event.is_directory and self._is_audio(event.src_path):
            self._on_deleted(event.src_path)
    
    def on_moved(self, event):
        # A file moved out of the watched directory (e.g. 'mv' to another
        # folder on the same filesystem) arrives as a move event rather
        # than a delete; a rename within the directory is delete + create.
        if event.is_directory or not self._is_audio(event.src_path):
            return
        self._on_deleted(event.src_path)
        dest = Path(event.dest_path)
        if dest.parent == self.directory and self._is_audio(dest):
            self._on_created(event.dest_path)


class FileWatcherThread(QThread):
    """Thread for watching directory changes"""
    file_added = pyqtSignal(str)
    file_deleted = pyqtSignal(str)
    
    def __init__(self, directory: str):
        super().__init__()
        self.directory = directory
        self.observer = None
        self._stop_requested = False
        
    def run(self):
        handler = FileWatcherHandler(
            self.directory, self.file_added.emit, self.file_deleted.emit)
        self.observer = Observer()
        self.observer.schedule(handler, self.directory, recursive=False)
        self.observer.start()
        
        # Keep thread alive
        self.exec()
        
        # Clean up when thread exits
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=1.0)
    
    def stop(self):
        self._stop_requested = True
        if self.observer:
            self.observer.stop()
        self.quit()


def parse_sync_url(url: str):
    """Parse a sync server URL like 'ssh://host:22/path/to/music'.

    Returns (host, port, path, user). Port defaults to 22, path to '/'.
    The scheme may be omitted; an optional 'user@' prefix is accepted.
    """
    url = (url or '').strip()
    if not url:
        return ('', 22, '/', '')
    if '://' not in url:
        url = 'ssh://' + url
    parts = urlsplit(url)
    host = parts.hostname or ''
    port = parts.port or 22
    path = parts.path or '/'
    user = parts.username or ''
    return (host, port, path, user)


class SyncWorker(QThread):
    """Background worker that syncs files to a remote SSH server.

    Operations (upload/remove) are placed on a queue and processed one at
    a time in this thread, so they never block the UI and never stack up
    on top of each other. The SSH connection is kept alive between
    operations and re-established automatically after failures.
    """
    log_message = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.queue = queue.Queue()
        self.enabled = False
        self.host = ''
        self.port = 22
        self.remote_dir = '/'
        self.user = ''
        self.auth = 'key'
        self.key_file = ''
        self.password = ''
        self._client = None
        self._sftp = None

    def update_config(self, enabled, url, user, auth, key_file, password):
        """Apply new settings. Safe to call from the main thread."""
        host, port, path, url_user = parse_sync_url(url)
        self.enabled = enabled
        self.host = host
        self.port = port
        self.remote_dir = path
        self.user = user or url_user
        self.auth = auth
        self.key_file = key_file
        self.password = password
        if not enabled:
            self._disconnect()

    def enqueue_upload(self, local_path: Path, remote_name: str):
        """Queue an upload of local_path to remote_dir/remote_name."""
        if not self.enabled:
            return
        self.queue.put(('upload', str(local_path), remote_name))

    def enqueue_remove(self, remote_name: str):
        """Queue a removal of remote_dir/remote_name (if it exists)."""
        if not self.enabled:
            return
        self.queue.put(('remove', None, remote_name))

    def _disconnect(self):
        try:
            if self._sftp is not None:
                self._sftp.close()
        except Exception:
            pass
        self._sftp = None
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        self._client = None

    def _ensure_connected(self):
        """Open (or reuse) the SSH connection to the remote server."""
        if self._sftp is not None:
            return
        if paramiko is None:
            raise RuntimeError("paramiko is not installed; add it to run remote sync")
        if not self.host:
            raise RuntimeError("No sync server configured")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(
            hostname=self.host,
            port=self.port,
            username=self.user,
            timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        if self.auth == 'key' and self.key_file:
            kwargs['key_filename'] = self.key_file
        elif self.password:
            kwargs['password'] = self.password
        client.connect(**kwargs)
        self._client = client
        self._sftp = client.open_sftp()

    def _remote_path(self, name: str) -> str:
        return f"{self.remote_dir.rstrip('/')}/{name}"

    def _ensure_remote_dir(self, path: str):
        """Create the remote directory tree if it does not exist yet."""
        current = ''
        for part in [p for p in path.split('/') if p]:
            current = f"{current}/{part}"
            try:
                self._sftp.stat(current)
            except IOError:
                try:
                    self._sftp.mkdir(current)
                except IOError:
                    pass  # exists or not creatable; put() will report it

    def run(self):
        while True:
            op = self.queue.get()
            if op is None:
                break
            kind, local_path, name = op
            try:
                self._ensure_connected()
                sftp = self._sftp  # local ref in case config changes mid-op
                if sftp is None:
                    raise RuntimeError("Connection lost")
                remote = self._remote_path(name)
                if kind == 'upload':
                    if not os.path.exists(local_path):
                        raise FileNotFoundError(f"Local file missing: {local_path}")
                    self._ensure_remote_dir(self.remote_dir)
                    sftp.put(local_path, remote)
                    self.log_message.emit(f"Synced '{name}' to remote")
                else:
                    try:
                        sftp.stat(remote)
                        sftp.remove(remote)
                        self.log_message.emit(f"Removed '{name}' from remote")
                    except IOError:
                        self.log_message.emit(f"'{name}' not on remote, nothing to remove")
            except Exception as e:
                self.log_message.emit(f"Sync error: {e}")
                self._disconnect()
            finally:
                self.queue.task_done()

    def stop(self):
        """Queue the stop sentinel and wait for the worker to finish."""
        self.queue.put(None)
        self.wait(3000)
        self._disconnect()


class PlaylistDelegate(QStyledItemDelegate):
    """Custom delegate to highlight the currently playing song and to draw
    the ♪ icon (gold + bold when the song is marked as saved)"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.playing_row = -1
        self.default_bg = QColor("#2d2d2d")
        self.playing_bg = QColor("#505050")  # Lighter gray
        self.beige_gold = QColor("#E8D4A0")
        self.white = QColor("#ffffff")
        self.gray = QColor("#888888")
        self.saved_gold = QColor("#ffd700")
    
    def set_playing_row(self, row: int):
        """Set which row is currently playing"""
        self.playing_row = row
    
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        """Custom paint to show playing song background"""
        painter.save()
        
        is_playing = (index.row() == self.playing_row)
        bg_color = self.playing_bg if is_playing else self.default_bg
        painter.fillRect(option.rect, bg_color)
        
        # FIXED: Use QStyle.StateFlag.State_Selected
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor("#4d9eff"))
        
        painter.restore()
        
        # The song-name column is drawn manually so the ♪ icon can be
        # colored/bolded per song
        if index.column() == 0:
            self._paint_song_cell(painter, option, index, is_playing)
            return
        
        modified_option = QStyleOptionViewItem(option)
        if is_playing:
            modified_option.palette.setColor(modified_option.palette.ColorRole.Text, self.beige_gold)
        else:
            modified_option.palette.setColor(modified_option.palette.ColorRole.Text, self.gray)
        
        super().paint(painter, modified_option, index)
    
    def _paint_song_cell(self, painter: QPainter, option: QStyleOptionViewItem,
                         index: QModelIndex, is_playing: bool):
        """Draw '♪ <name>' for the song column; the ♪ is gold + bold when
        the song is marked as saved."""
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or '')
        saved = bool(index.data(Qt.ItemDataRole.UserRole + 1))
        
        font = index.data(Qt.ItemDataRole.FontRole)
        if font is None or not font.isValid():
            font = self.parent().font()
        
        name_color = self.beige_gold if is_playing else self.white
        icon_color = self.saved_gold if saved else name_color
        
        rect = option.rect.adjusted(4, 0, -4, 0)
        
        icon_font = QFont(font)
        icon_font.setBold(saved)
        painter.setFont(icon_font)
        icon_width = painter.fontMetrics().horizontalAdvance('♪') + 6
        icon_rect = QRect(rect)
        icon_rect.setWidth(icon_width)
        painter.setPen(icon_color)
        painter.drawText(icon_rect,
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         '♪')
        
        name_rect = QRect(rect)
        name_rect.setLeft(rect.left() + icon_width)
        painter.setFont(font)
        painter.setPen(name_color)
        text = painter.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, max(0, name_rect.width()))
        painter.drawText(name_rect,
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         text)


class SettingsDialog(QDialog):
    """Settings dialog for configuring Musibisk"""

    def __init__(self, parent=None, initial_songs=50, play_order=PlayOrder.OLDEST_TO_NEWEST,
                 sync_enabled=False, sync_url='', sync_user='', sync_auth='key',
                 sync_key_file='', sync_password=''):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setFixedSize(400, 348)

        layout = QFormLayout(self)

        self.songs_spinbox = QSpinBox()
        self.songs_spinbox.setRange(1, 1000)
        self.songs_spinbox.setValue(initial_songs)
        self.songs_spinbox.setSuffix(" songs")

        layout.addRow("Initial playlist size:", self.songs_spinbox)

        # Play order dropdown
        self.play_order_combo = QComboBox()
        self.play_order_combo.addItem("Oldest to Newest", PlayOrder.OLDEST_TO_NEWEST)
        self.play_order_combo.addItem("Newest to Oldest", PlayOrder.NEWEST_TO_OLDEST)
        self.play_order_combo.setCurrentIndex(play_order.value)

        layout.addRow("Play order:", self.play_order_combo)

        # Remote sync section
        sync_header = QLabel("Remote sync")
        sync_header.setStyleSheet("font-weight: bold; color: #E8D4A0; padding-top: 8px;")
        layout.addRow(sync_header)

        self.sync_check = QCheckBox("Enable remote sync")
        self.sync_check.setChecked(sync_enabled)

        layout.addRow(self.sync_check)

        self.sync_url_edit = QLineEdit(sync_url)
        self.sync_url_edit.setPlaceholderText("ssh://192.168.1.100:22/path/to/music")

        layout.addRow("Server:", self.sync_url_edit)

        self.sync_user_edit = QLineEdit(sync_user)

        layout.addRow("User:", self.sync_user_edit)

        self.sync_auth_combo = QComboBox()
        self.sync_auth_combo.addItem("SSH key file", 'key')
        self.sync_auth_combo.addItem("Password", 'password')
        self.sync_auth_combo.setCurrentIndex(0 if sync_auth == 'key' else 1)
        self.sync_auth_combo.currentIndexChanged.connect(self._update_auth_fields)

        layout.addRow("Auth:", self.sync_auth_combo)

        self.sync_key_file_edit = QLineEdit(sync_key_file)
        self.sync_key_file_edit.setReadOnly(True)
        self.sync_key_file_edit.setPlaceholderText("Path to SSH private key (e.g. ~/.ssh/id_ed25519)")
        key_file_row = QWidget()
        key_file_layout = QHBoxLayout(key_file_row)
        key_file_layout.setContentsMargins(0, 0, 0, 0)
        key_file_layout.addWidget(self.sync_key_file_edit)
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self.browse_key_file)
        key_file_layout.addWidget(browse_button)
        self.key_file_row = key_file_row

        layout.addRow("Key file:", self.key_file_row)

        self.sync_password_edit = QLineEdit(sync_password)
        self.sync_password_edit.setEchoMode(QLineEdit.EchoMode.Password)

        layout.addRow("Password:", self.sync_password_edit)

        self._update_auth_fields()

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        self.apply_style()

    def browse_key_file(self):
        """Open a file dialog to pick an SSH private key file"""
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Select SSH Key File", str(Path.home()),
            "Key Files (*.pem *.ppk *.key id_rsa id_ecdsa id_ed25519);;All Files (*)"
        )
        if filepath:
            self.sync_key_file_edit.setText(filepath)

    def _update_auth_fields(self):
        """Show only the fields relevant to the selected auth method"""
        is_key = self.sync_auth_combo.currentData() == 'key'
        self.key_file_row.setVisible(is_key)
        self.sync_password_edit.setVisible(not is_key)
    
    def apply_style(self):
        """Apply dark theme to dialog"""
        self.setStyleSheet("""
            QDialog {
                background-color: #1e1e1e;
                color: #ffffff;
            }
            QLabel {
                color: #ffffff;
            }
            QSpinBox, QComboBox, QLineEdit {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                border-radius: 4px;
                padding: 4px;
            }
            QCheckBox {
                color: #ffffff;
                spacing: 6px;
            }
            QCheckBox::indicator {
                width: 14px;
                height: 14px;
                background-color: #2d2d2d;
                border: 1px solid #3d3d3d;
                border-radius: 3px;
            }
            QCheckBox::indicator:checked {
                background-color: #4d9eff;
                border: 1px solid #4d9eff;
            }
            QSpinBox::up-button, QSpinBox::down-button {
                background-color: #3d3d3d;
                border: 1px solid #4d4d4d;
            }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {
                background-color: #4d4d4d;
            }
            QComboBox::drop-down {
                border: none;
                background-color: #3d3d3d;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid #ffffff;
                margin-right: 5px;
            }
            QComboBox QAbstractItemView {
                background-color: #2d2d2d;
                color: #ffffff;
                selection-background-color: #4d9eff;
                border: 1px solid #3d3d3d;
            }
            QPushButton {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                border-radius: 4px;
                padding: 6px 12px;
            }
            QPushButton:hover {
                background-color: #3d3d3d;
            }
        """)
    
    def get_songs_count(self):
        """Return the selected number of songs"""
        return self.songs_spinbox.value()

    def get_play_order(self):
        """Return the selected play order"""
        return self.play_order_combo.currentData()

    def get_sync_enabled(self):
        return self.sync_check.isChecked()

    def get_sync_url(self):
        return self.sync_url_edit.text().strip()

    def get_sync_user(self):
        return self.sync_user_edit.text().strip()

    def get_sync_auth(self):
        return self.sync_auth_combo.currentData()

    def get_sync_key_file(self):
        return self.sync_key_file_edit.text().strip()

    def get_sync_password(self):
        return self.sync_password_edit.text()


class GlyphCenteredButton(QPushButton):
    """QPushButton that keeps its text glyph vertically centered.

    Emoji glyphs (e.g. from Noto Color Emoji) and text glyphs (e.g. from
    DejaVu Sans) are placed at different heights within the font's em box, so
    the same button can look slightly high or low depending on which glyph is
    shown and which fonts are installed. The button renders its current text,
    measures the glyph's bounding box, and compensates with padding-top (glyph
    too high) or padding-bottom (glyph too low). The compensation is
    re-applied whenever the text or the stylesheet changes, so buttons whose
    glyph changes (play/pause, loop mode) stay centered after the swap.
    """

    def __init__(self, text="", parent=None):
        self._glyph_padding = (0, 0)
        self._base_sheet = ""
        super().__init__(text, parent)

    def setStyleSheet(self, sheet):
        self._base_sheet = sheet
        super().setStyleSheet(self._sheet_with_padding())
        self._recenter_glyph()

    def setText(self, text):
        super().setText(text)
        self._recenter_glyph()

    def showEvent(self, event):
        super().showEvent(event)
        # The effective font (QSS font-size, inherited weight, DPI) is only
        # final once the widget is shown; re-measure so the compensation is
        # computed in the same context the button renders in.
        self._recenter_glyph()

    def _sheet_with_padding(self):
        """Base stylesheet plus the managed padding rules."""
        top, bottom = self._glyph_padding
        if not top and not bottom:
            return self._base_sheet
        rules = []
        if top:
            rules.append(f"padding-top: {top}px;")
        if bottom:
            rules.append(f"padding-bottom: {bottom}px;")
        extra = " ".join(rules)
        base = self._base_sheet
        if not base:
            return extra
        if "{" in base:
            # Insert the managed rules inside the first rule block.
            return re.sub(r"\{", "{ " + extra + " ", base, count=1)
        return f"{base} {extra}"

    def _recenter_glyph(self):
        """Measure the current glyph's offset from center and re-apply padding."""
        text = self.text()
        if not text:
            return

        rect = self.contentsRect()
        if rect.width() <= 0 or rect.height() <= 0:
            return

        pixmap = QPixmap(rect.size())
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setFont(self.font())
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.end()

        image = pixmap.toImage()
        dpr = pixmap.devicePixelRatio()
        min_row = max_row = None
        for row in range(image.height()):
            for col in range(image.width()):
                if image.pixelColor(col, row).alpha() > 20:
                    if min_row is None:
                        min_row = row
                    max_row = row
                    break
        if min_row is None:
            return

        offset = ((min_row + max_row) / 2) / dpr - rect.height() / 2
        if offset < -0.75:
            padding = (int(round(-2 * offset)), 0)   # too high -> push down
        elif offset > 0.75:
            padding = (0, int(round(2 * offset)))    # too low -> push up
        else:
            padding = (0, 0)
        if padding == self._glyph_padding:
            return
        self._glyph_padding = padding
        super().setStyleSheet(self._sheet_with_padding())


class TrackInfoPanel(QWidget):
    """Right-side panel that displays the current track's metadata.

    Part of the main window, docked to the right of the main content behind
    a thin vertical separator (the menu bar row only covers the left column,
    so it stops at that separator). Shows cover art, title, artist, album,
    year and genre when present in the file metadata. The content is
    vertically centered in the panel.
    """
    WIDTH = 260
    ART_SIZE = 220
    ART_RADIUS = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_path: Optional[Path] = None

        self.setFixedWidth(self.WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        layout.addStretch(1)

        # Cover art (or a placeholder glyph when the track has none)
        self.cover_label = QLabel(self)
        self.cover_label.setFixedSize(self.ART_SIZE, self.ART_SIZE)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet(
            "background-color: #2d2d2d; border-radius: "
            f"{self.ART_RADIUS}px;")
        self.cover_glyph = QLabel("♫", self.cover_label)
        self.cover_glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_glyph.setStyleSheet("font-size: 72px; color: #444444;")
        layout.addWidget(self.cover_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self.title_label = self._make_label(15, bold=True)
        self.artist_label = self._make_label(13, color="#dddddd")
        self.album_label = self._make_label(12, color="#999999", italic=True)
        self.extra_label = self._make_label(11, color="#777777")
        for lbl in (self.title_label, self.artist_label,
                    self.album_label, self.extra_label):
            lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            lbl.setWordWrap(True)
            layout.addWidget(lbl, 0, Qt.AlignmentFlag.AlignHCenter)

        layout.addStretch(1)

        self.clear()

    def _make_label(self, size: int, bold: bool = False,
                    color: str = None, italic: bool = False) -> QLabel:
        lbl = QLabel(self)
        style = f"font-size: {size}px;"
        if bold:
            style += " font-weight: bold;"
        if italic:
            style += " font-style: italic;"
        if color:
            style += f" color: {color};"
        lbl.setStyleSheet(style)
        return lbl

    def _set_field(self, lbl: QLabel, text):
        """Show the label with the given text, or hide it when empty."""
        if text:
            lbl.setText(str(text))
            lbl.setVisible(True)
        else:
            lbl.setText('')
            lbl.setVisible(False)

    def set_track(self, filepath: Path):
        """Populate the panel from the file's metadata."""
        self.current_path = filepath
        info = read_track_info(filepath)

        self.title_label.setText(info['title'] or filepath.stem)
        self.title_label.setVisible(True)
        self._set_field(self.artist_label, info['artist'])
        self._set_field(self.album_label, info['album'])
        extra = ' · '.join(x for x in (info['year'], info['genre']) if x)
        self._set_field(self.extra_label, extra or None)

        cover = info['cover']
        if cover:
            pixmap = QPixmap()
            fmt = cover[1].split('/')[-1].upper()
            pixmap.loadFromData(QByteArray(cover[0]), fmt)
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    self.ART_SIZE, self.ART_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self.cover_label.setPixmap(self._round_pixmap(scaled))
                self.cover_glyph.setVisible(False)
                return
        self.cover_label.setPixmap(QPixmap())
        self.cover_glyph.setVisible(True)

    def clear(self):
        """Reset the panel to the 'no track' state."""
        self.current_path = None
        self.title_label.setText('No track loaded')
        self.title_label.setVisible(True)
        for lbl in (self.artist_label, self.album_label, self.extra_label):
            self._set_field(lbl, None)
        self.cover_label.setPixmap(QPixmap())
        self.cover_glyph.setVisible(True)

    def _round_pixmap(self, pixmap: QPixmap) -> QPixmap:
        """Return a copy of the pixmap with anti-aliased rounded corners,
        matching the placeholder box's radius."""
        mask = QPixmap(pixmap.size())
        mask.fill(Qt.GlobalColor.transparent)
        painter = QPainter(mask)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(
            0, 0, pixmap.width(), pixmap.height(),
            self.ART_RADIUS, self.ART_RADIUS)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        return mask


class Musibisk(QMainWindow):
    """Main application window"""
    
    CONFIG_DIR = Path.home() / '.config' / 'musibisk'
    CONFIG_FILE = CONFIG_DIR / 'config.json'
    AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.opus', '.aac', '.wma'}
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Musibisk")
        # 530 (content) + 1 (separator) + 260 (track info panel)
        self.setFixedSize(791, 400)
        
        # State
        self.playlist: List[Path] = []
        self.current_index: int = -1
        self.loop_mode = LoopMode.NO_LOOP
        self.play_order = PlayOrder.OLDEST_TO_NEWEST
        self.target_directory: Optional[Path] = None
        self.watcher_thread: Optional[FileWatcherThread] = None
        self.initial_songs_count: int = 50
        
        # Remote sync state
        self.sync_enabled: bool = False
        self.sync_url: str = ''
        self.sync_user: str = ''
        self.sync_auth: str = 'key'
        self.sync_key_file: str = ''
        self.sync_password: str = ''
        self.sync_worker: Optional[SyncWorker] = None
        self._status_hide_timer: Optional[QTimer] = None
        
        # Delete button state
        self.delete_click_count = 0
        self.delete_last_click_time = 0
        self.delete_last_song_index = -1
        
        # Media player
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.7)
        
        # Connect signals
        self.player.positionChanged.connect(self.update_position)
        self.player.durationChanged.connect(self.update_duration)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        
        # Setup UI
        self.init_ui()
        self.apply_style()
        
        # Load config
        self.load_config()
        
        # Setup global hotkeys
        self.setup_global_hotkeys()
        
        # Start background sync worker and apply loaded sync settings
        self.sync_worker = SyncWorker()
        self.sync_worker.log_message.connect(self.on_sync_log)
        self.sync_worker.start()
        self.apply_sync_config()
    
    def init_ui(self):
        """Initialize the user interface"""
        # File menu (popped up from a button in the left column's menu row,
        # so the menu row stops at the separator before the info panel)
        file_menu = QMenu(self)
        
        select_folder_action = QAction("Select Target Folder", self)
        select_folder_action.triggered.connect(self.select_folder)
        file_menu.addAction(select_folder_action)
        
        file_menu.addSeparator()
        
        self.sync_menu_action = QAction("Remote Sync", self)
        self.sync_menu_action.setCheckable(True)
        self.sync_menu_action.setToolTip("Toggle syncing saved songs to the remote server")
        self.sync_menu_action.triggered.connect(self.toggle_remote_sync)
        file_menu.addAction(self.sync_menu_action)
        
        file_menu.addSeparator()
        
        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self.show_settings)
        file_menu.addAction(settings_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # Central widget: [ left content | separator | track info panel ]
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        root_layout = QHBoxLayout(central_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        
        # Menu bar row (left column only)
        menu_row = QWidget()
        menu_row.setFixedHeight(26)
        menu_row.setStyleSheet(
            "background-color: #2d2d2d; border-bottom: 1px solid #3d3d3d;")
        menu_row_layout = QHBoxLayout(menu_row)
        menu_row_layout.setContentsMargins(0, 0, 0, 0)
        menu_row_layout.setSpacing(0)
        self.file_button = QToolButton(menu_row)
        self.file_button.setText("File")
        self.file_button.setMenu(file_menu)
        self.file_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self.file_button.setArrowType(Qt.ArrowType.NoArrow)
        self.file_button.setStyleSheet(
            "QToolButton { background-color: transparent; color: #ffffff;"
            " border: none; padding: 0 12px; font-size: 13px; }"
            "QToolButton:hover { background-color: #3d3d3d; }")
        menu_row_layout.addWidget(self.file_button)
        menu_row_layout.addStretch()
        left_layout.addWidget(menu_row)
        
        # Main content (left column)
        layout = QVBoxLayout()
        layout.setContentsMargins(8, 5, 8, 8)
        layout.setSpacing(8)
        
        # Directory label at top
        self.dir_label = QLabel("No directory selected")
        self.dir_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dir_label.setStyleSheet(f"font-size: 10px; color: #888; padding: 2px; font-family: {BitmapFontFamily};")
        self.dir_label.setMaximumHeight(16)
        layout.addWidget(self.dir_label)
        
        # Playlist view - now using QTableWidget
        self.playlist_widget = QTableWidget()
        self.playlist_widget.setColumnCount(3)
        self.playlist_widget.setHorizontalHeaderLabels(["Song", "Length", "Modified"])
        self.playlist_widget.setStyleSheet(f"font-family: {BitmapFontFamily};")
        self.playlist_widget.cellDoubleClicked.connect(self.on_playlist_item_clicked)
        self.playlist_widget.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.playlist_widget.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.playlist_widget.verticalHeader().setVisible(False)
        self.playlist_widget.setShowGrid(False)
        
        self.playlist_delegate = PlaylistDelegate(self.playlist_widget)
        self.playlist_widget.setItemDelegate(self.playlist_delegate)
        
        # Set column widths
        header = self.playlist_widget.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.playlist_widget.setColumnWidth(1, 60)   # Fixed width for length column
        self.playlist_widget.setColumnWidth(2, 150)  # Fixed width for timestamp column
        
        layout.addWidget(self.playlist_widget)
        
        # Song info
        self.song_label = QLabel("No song loaded")
        self.song_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.song_label.setStyleSheet(f"font-size: 14px; font-weight: bold; padding: 4px 0; font-family: {BitmapFontFamily};")
        self.song_label.setMaximumHeight(30)
        layout.addWidget(self.song_label)
        
        # Time info
        time_layout = QHBoxLayout()
        time_layout.setContentsMargins(0, 0, 0, 0)
        self.time_label = QLabel("0:00")
        self.time_label.setStyleSheet(f"font-size: 11px; color: #aaa; font-family: {BitmapFontFamily};")
        self.duration_label = QLabel("0:00")
        self.duration_label.setStyleSheet(f"font-size: 11px; color: #aaa; font-family: {BitmapFontFamily};")
        time_layout.addWidget(self.time_label)
        time_layout.addStretch()
        time_layout.addWidget(self.duration_label)
        layout.addLayout(time_layout)
        
        # Seek bar - using custom ClickableSlider
        self.seek_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self.seek)
        self.seek_slider.setMaximumHeight(20)
        layout.addWidget(self.seek_slider)
        
        # Add spacing before buttons
        layout.addSpacing(5)
        
        # Control buttons - ALL THE SAME SIZE
        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(10)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        
        button_size = 45  # All buttons same size
        
        self.prev_button = GlyphCenteredButton("⏮")
        self.prev_button.setStyleSheet(BUTTON_FONT_SIZE)
        self.prev_button.setFixedSize(button_size, button_size)
        self.prev_button.clicked.connect(self.previous_song)
        
        self.play_pause_button = GlyphCenteredButton("▶")
        self.play_pause_button.setStyleSheet(BUTTON_FONT_SIZE)
        self.play_pause_button.setFixedSize(button_size, button_size)
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        
        self.next_button = GlyphCenteredButton("⏭")
        self.next_button.setStyleSheet(BUTTON_FONT_SIZE)
        self.next_button.setFixedSize(button_size, button_size)
        self.next_button.clicked.connect(self.next_song)
        
        self.loop_button = GlyphCenteredButton("🔁")
        self.loop_button.setStyleSheet(BUTTON_FONT_SIZE)
        self.loop_button.setFixedSize(button_size, button_size)
        self.loop_button.clicked.connect(self.toggle_loop_mode)
        self.update_loop_button()
        
        # Add vertical separator
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        separator.setStyleSheet("color: #3d3d3d;")
        separator.setFixedHeight(button_size)
        
        # Save button (floppy disk icon)
        self.save_button = GlyphCenteredButton("💾")
        self.save_button.setStyleSheet(BUTTON_FONT_SIZE)
        self.save_button.setFixedSize(button_size, button_size)
        self.save_button.clicked.connect(self.toggle_save_song)
        self.save_button.setToolTip("Save/unsave current song")
        
        # Delete button
        self.delete_button = GlyphCenteredButton("🗑")
        self.delete_button.setStyleSheet(BUTTON_FONT_SIZE)
        self.delete_button.setFixedSize(button_size, button_size)
        self.delete_button.clicked.connect(self.handle_delete_click)
        self.delete_button.setToolTip("Double-click to delete song")
        
        self.volume_slider = QSlider(Qt.Orientation.Vertical)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(int(self.audio_output.volume() * 100))
        self.volume_slider.valueChanged.connect(self.handle_volume_slider)
        self.volume_slider.setFixedWidth(20)
        self.volume_slider.setFixedHeight(45)

        self.volume_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                background: #333;
                height: 4px;
                border-radius: 2px;
            }

            QSlider::handle:horizontal {
                background: #4CAF50;
                width: 10px;
                border-radius: 5px;
            }
            """)
        
        controls_layout.addStretch()
        controls_layout.addWidget(self.prev_button)
        controls_layout.addWidget(self.play_pause_button)
        controls_layout.addWidget(self.next_button)
        controls_layout.addWidget(self.loop_button)
        controls_layout.addWidget(separator)
        controls_layout.addWidget(self.save_button)
        controls_layout.addWidget(self.delete_button)
        controls_layout.addWidget(separator)
        controls_layout.addWidget(self.volume_slider)
        controls_layout.addStretch()
        
        layout.addLayout(controls_layout)
        
        left_layout.addLayout(layout)
        
        # Thin vertical separator between the content and the info panel
        panel_separator = QWidget()
        panel_separator.setFixedWidth(1)
        panel_separator.setStyleSheet("background-color: #3d3d3d;")
        
        # Track info panel (right side)
        self.info_panel = TrackInfoPanel(left_widget)
        
        root_layout.addWidget(left_widget, 1)
        root_layout.addWidget(panel_separator)
        root_layout.addWidget(self.info_panel)
    
    def apply_style(self):
        """Apply dark theme styling"""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e1e;
            }
            QWidget {
                background-color: #1e1e1e;
                color: #ffffff;
            }
            QMenuBar {
                background-color: #2d2d2d;
                color: #ffffff;
                border-bottom: 1px solid #3d3d3d;
                padding: 2px;
            }
            QMenuBar::item {
                padding: 4px 8px;
            }
            QMenuBar::item:selected {
                background-color: #3d3d3d;
            }
            QMenu {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3d3d3d;
            }
            QMenu::item {
                padding: 6px 20px;
            }
            QMenu::item:selected {
                background-color: #3d3d3d;
            }
            QTableWidget {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                border-radius: 6px;
                padding: 4px;
                font-size: 11px;
            }
            QTableWidget::item {
                padding: 4px;
                border-radius: 3px;
            }
            QTableWidget::item:selected {
                background-color: #4d9eff;
                color: #ffffff;
            }
            QHeaderView::section {
                background-color: #2d2d2d;
                color: #aaa;
                border: none;
                padding: 4px;
                font-size: 10px;
                font-weight: bold;
            }
            QPushButton {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                border-radius: 10px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3d3d3d;
                border: 1px solid #4d4d4d;
            }
            QPushButton:pressed {
                background-color: #1a1a1a;
            }
            QSlider::groove:horizontal {
                border: 1px solid #3d3d3d;
                height: 6px;
                background: #2d2d2d;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #ffffff;
                border: 1px solid #3d3d3d;
                width: 14px;
                margin: -5px 0;
                border-radius: 7px;
            }
            QSlider::handle:horizontal:hover {
                background: #e0e0e0;
            }
            QSlider::sub-page:horizontal {
                background: #4d9eff;
                border-radius: 3px;
            }
            QScrollBar:vertical:goove {
                background-color: #1e1e1e;
                width: 12px;
                border-radius: 4px;
                border: 1px solid #3d3d3d;
            }
            QScrollBar:vertical {
                background-color: #1e1e1e;
                width: 12px;
                border-radius: 4px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background-color: #4d4d4d;
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #5d5d5d;
            }
            QScrollBar::handle:vertical:pressed {
                background-color: #6d6d6d;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: none;
            }
            QScrollBar:horizontal {
                background-color: #1e1e1e;
                height: 12px;
                border-radius: 6px;
                margin: 0px;
            }
            QScrollBar::handle:horizontal {
                background-color: #4d4d4d;
                border-radius: 6px;
                min-width: 20px;
            }
            QScrollBar::handle:horizontal:hover {
                background-color: #5d5d5d;
            }
            QScrollBar::handle:horizontal:pressed {
                background-color: #6d6d6d;
            }
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {
                width: 0px;
            }
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {
                background: none;
            }
        """)
    
    def _update_track_info(self):
        """Refresh the track info panel for the current (or absent) track."""
        if self.info_panel is None:
            return
        if 0 <= self.current_index < len(self.playlist):
            self.info_panel.set_track(self.playlist[self.current_index])
        else:
            self.info_panel.clear()

    def setup_global_hotkeys(self):
        """Setup global hotkeys for media control"""
        # Note: PyQt6 doesn't have native global hotkeys
        # We'll use keyboard shortcuts that work when the app has focus
        # For true global hotkeys, you'd need platform-specific libraries
        
        # Media keys should work globally on most systems
        play_pause_shortcut = QAction(self)
        play_pause_shortcut.setShortcut(QKeySequence("Media Play"))
        play_pause_shortcut.triggered.connect(self.toggle_play_pause)
        self.addAction(play_pause_shortcut)
        
        next_shortcut = QAction(self)
        next_shortcut.setShortcut(QKeySequence("Media Next"))
        next_shortcut.triggered.connect(self.next_song)
        self.addAction(next_shortcut)
        
        prev_shortcut = QAction(self)
        prev_shortcut.setShortcut(QKeySequence("Media Previous"))
        prev_shortcut.triggered.connect(self.previous_song)
        self.addAction(prev_shortcut)
        
        # Alternative keyboard shortcuts
        self.addAction(self.create_shortcut("Space", self.toggle_play_pause))
        self.addAction(self.create_shortcut("Right", self.next_song))
        self.addAction(self.create_shortcut("Left", self.previous_song))
    
    def create_shortcut(self, key: str, callback):
        """Helper to create keyboard shortcuts"""
        action = QAction(self)
        action.setShortcut(QKeySequence(key))
        action.triggered.connect(callback)
        return action
    
    def select_folder(self):
        """Open folder selection dialog"""
        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Music Folder",
            str(self.target_directory) if self.target_directory else str(Path.home())
        )
        
        if directory:
            self.set_target_directory(Path(directory))
    
    def show_settings(self):
        """Show settings dialog"""
        dialog = SettingsDialog(
            self,
            self.initial_songs_count,
            self.play_order,
            sync_enabled=self.sync_enabled,
            sync_url=self.sync_url,
            sync_user=self.sync_user,
            sync_auth=self.sync_auth,
            sync_key_file=self.sync_key_file,
            sync_password=self.sync_password,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            old_play_order = self.play_order
            self.initial_songs_count = dialog.get_songs_count()
            self.play_order = dialog.get_play_order()
            self.sync_enabled = dialog.get_sync_enabled()
            self.sync_url = dialog.get_sync_url()
            self.sync_user = dialog.get_sync_user()
            self.sync_auth = dialog.get_sync_auth()
            self.sync_key_file = dialog.get_sync_key_file()
            self.sync_password = dialog.get_sync_password()
            self.apply_sync_config()
            self.save_config()
            
            # Reload playlist if directory is set
            if self.target_directory:
                self.load_existing_files(self.target_directory)
    
    def set_target_directory(self, directory: Path):
        """Set the target directory and start monitoring"""
        self.target_directory = directory
        self.dir_label.setText(f"📁 {directory.name}")
        
        # Stop existing watcher
        if self.watcher_thread:
            self.watcher_thread.stop()
            self.watcher_thread.wait()
        
        # Load existing files
        self.load_existing_files(directory)
        
        # Start new watcher
        self.watcher_thread = FileWatcherThread(str(directory))
        self.watcher_thread.file_added.connect(self.add_file_to_playlist)
        self.watcher_thread.file_deleted.connect(self.remove_file_from_playlist)
        self.watcher_thread.start()
        
        # Save config
        self.save_config()
    
    def get_next_index(self):
        """Get the next song index based on play order"""
        if not self.playlist:
            return -1
        
        if self.play_order == PlayOrder.OLDEST_TO_NEWEST:
            # Moving forward through the list (bottom to top in display)
            return (self.current_index + 1) % len(self.playlist)
        else:  # NEWEST_TO_OLDEST
            # Moving backward through the list (top to bottom in display)
            next_idx = self.current_index - 1
            if next_idx < 0:
                next_idx = len(self.playlist) - 1
            return next_idx
    
    def get_previous_index(self):
        """Get the previous song index based on play order"""
        if not self.playlist:
            return -1
        
        if self.play_order == PlayOrder.OLDEST_TO_NEWEST:
            # Moving backward through the list (top to bottom in display)
            prev_idx = self.current_index - 1
            if prev_idx < 0:
                prev_idx = len(self.playlist) - 1
            return prev_idx
        else:  # NEWEST_TO_OLDEST
            # Moving forward through the list (bottom to top in display)
            return (self.current_index + 1) % len(self.playlist)
    
    def get_starting_index(self):
        """Get the index to start playing from based on play order"""
        if not self.playlist:
            return -1
        
        if self.play_order == PlayOrder.OLDEST_TO_NEWEST:
            # Start at the end (oldest song, which is at bottom)
            return len(self.playlist) - 1
        else:  # NEWEST_TO_OLDEST
            # Start at the beginning (newest song, which is at top)
            return 0
    
    def load_existing_files(self, directory: Path, limit: Optional[int] = None):
        """Load existing audio files from directory"""
        if limit is None:
            limit = self.initial_songs_count
            
        files = []
        for ext in self.AUDIO_EXTENSIONS:
            files.extend(directory.glob(f"*{ext}"))
        
        # Sort by modification time, most recent first
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        
        # Take the N most recent
        files = files[:limit]
        
        # Clear playlist and add files
        self.playlist.clear()
        self.playlist_widget.setRowCount(0)
        
        # Add files in order (most recent first, so they appear at top)
        for file in files:
            self.playlist.append(file)
            self.add_to_playlist_widget(file)
        
        # Start playing from the appropriate position based on play order
        if self.playlist and self.current_index == -1:
            self.current_index = self.get_starting_index()
            self.load_current_song()
            self.highlight_current_song()
    
    def add_file_to_playlist(self, filepath: str):
        """Add a new file to the playlist"""
        path = Path(filepath)
        if path not in self.playlist:
            # Insert at the beginning (top) of the playlist
            self.playlist.insert(0, path)
            self.add_to_playlist_widget_at_top(path)
            
            # Adjust current index if necessary
            if self.current_index >= 0:
                self.current_index += 1
                # Update the highlighted row to match the new index
                self.highlight_current_song()
            
            # If nothing is playing, start playing from the appropriate position
            if self.current_index == -1:
                self.current_index = self.get_starting_index()
                self.load_current_song()
                self.highlight_current_song()
                self.player.play()
    
    def remove_file_from_playlist(self, filepath: str):
        """Remove a file from the playlist (it was deleted/moved out of the
        watched directory outside of the app). No-op if already removed —
        the in-app delete flow removes the entry before its event arrives."""
        path = Path(filepath)
        if path not in self.playlist:
            return
        
        index = self.playlist.index(path)
        was_current = (index == self.current_index)
        
        # Keep the remote consistent: if it was a saved song, remove it
        # from the server (no-op there if it doesn't exist). The file is
        # already gone, so the saved state comes from the row's cached
        # flag (kept in sync by toggle_save_song), not the tag.
        item = self.playlist_widget.item(index, 0)
        was_saved = (bool(item.data(Qt.ItemDataRole.UserRole + 1))
                     if item is not None else self.is_song_saved(path))
        if was_saved:
            self.sync_remove(self.get_remote_name(path))
        
        del self.playlist[index]
        self.playlist_widget.removeRow(index)
        
        if was_current:
            self.player.stop()
            if self.playlist:
                # The next song slid into this index
                if self.current_index >= len(self.playlist):
                    self.current_index = 0
                self.load_current_song()
                self.player.play()
                self.play_pause_button.setText("⏸")
            else:
                self.current_index = -1
                self.song_label.setText("No song loaded")
                self.play_pause_button.setText("▶")
                self._update_track_info()
        else:
            if index < self.current_index:
                self.current_index -= 1
            self.highlight_current_song()
    
    def get_formatted_timestamp(self, filepath: Path) -> str:
        """Get formatted timestamp for file modification time"""
        try:
            mtime = filepath.stat().st_mtime
            dt = datetime.fromtimestamp(mtime)
            return dt.strftime("%H:%M:%S %m/%d/%Y")
        except:
            return "Unknown"

    def get_song_length(self, filepath: Path) -> str:
        """Get formatted length (M:SS) of an audio file"""
        try:
            audio = mutagen.File(filepath)
            if audio and audio.info:
                return self.format_time(int(audio.info.length * 1000))
        except:
            pass
        return "?"
    
    def add_to_playlist_widget(self, filepath: Path):
        """Add a song to the playlist widget (at the end)"""
        song_name = self.get_song_name(filepath)
        length = self.get_song_length(filepath)
        timestamp = self.get_formatted_timestamp(filepath)
        
        row = self.playlist_widget.rowCount()
        self.playlist_widget.insertRow(row)
        
        # Song name column (the ♪ icon is drawn by the delegate; the saved
        # flag in UserRole+1 makes it gold+bold)
        song_item = QTableWidgetItem(song_name)
        song_item.setData(Qt.ItemDataRole.UserRole, filepath)
        song_item.setData(Qt.ItemDataRole.UserRole + 1,
                          1 if self.is_song_saved(filepath) else 0)
        song_item.setFlags(song_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.playlist_widget.setItem(row, 0, song_item)
        
        # Length column
        length_item = QTableWidgetItem(length)
        length_item.setForeground(Qt.GlobalColor.gray)
        length_item.setFlags(length_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.playlist_widget.setItem(row, 1, length_item)
        
        # Timestamp column
        time_item = QTableWidgetItem(timestamp)
        time_item.setForeground(Qt.GlobalColor.gray)
        time_item.setFlags(time_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.playlist_widget.setItem(row, 2, time_item)
    
    def add_to_playlist_widget_at_top(self, filepath: Path):
        """Add a song to the playlist widget at the top"""
        song_name = self.get_song_name(filepath)
        length = self.get_song_length(filepath)
        timestamp = self.get_formatted_timestamp(filepath)
        
        self.playlist_widget.insertRow(0)
        
        # Song name column (the ♪ icon is drawn by the delegate; the saved
        # flag in UserRole+1 makes it gold+bold)
        song_item = QTableWidgetItem(song_name)
        song_item.setData(Qt.ItemDataRole.UserRole, filepath)
        song_item.setData(Qt.ItemDataRole.UserRole + 1,
                          1 if self.is_song_saved(filepath) else 0)
        song_item.setFlags(song_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.playlist_widget.setItem(0, 0, song_item)
        
        # Length column
        length_item = QTableWidgetItem(length)
        length_item.setForeground(Qt.GlobalColor.gray)
        length_item.setFlags(length_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.playlist_widget.setItem(0, 1, length_item)
        
        # Timestamp column
        time_item = QTableWidgetItem(timestamp)
        time_item.setForeground(Qt.GlobalColor.gray)
        time_item.setFlags(time_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.playlist_widget.setItem(0, 2, time_item)
    
    def on_playlist_item_clicked(self, row: int, column: int):
        """Handle playlist item double-click"""
        item = self.playlist_widget.item(row, 0)
        if item:
            filepath = item.data(Qt.ItemDataRole.UserRole)
            try:
                index = self.playlist.index(filepath)
                
                # Store current scroll position
                scrollbar = self.playlist_widget.verticalScrollBar()
                scroll_pos = scrollbar.value()
                
                self.current_index = index
                self.load_current_song()
                self.highlight_current_song()
                self.player.play()
                self.play_pause_button.setText("⏸")
                
                # Restore scroll position to prevent auto-scroll
                scrollbar.setValue(scroll_pos)
                
                # Reset delete click counter when changing songs
                self.reset_delete_state()
            except ValueError:
                pass
    
    def highlight_current_song(self):
        """Highlight the currently playing song in the playlist"""
        self.playlist_delegate.set_playing_row(self.current_index)
        self.playlist_widget.viewport().update()

    
    def load_current_song(self):
        """Load the current song into the player"""
        if 0 <= self.current_index < len(self.playlist):
            filepath = self.playlist[self.current_index]
            self.player.setSource(QUrl.fromLocalFile(str(filepath)))
            
            # Update song label with metadata or filename
            song_name = self.get_song_name(filepath)
            self.song_label.setText(song_name)
            
            # Highlight in playlist
            self.highlight_current_song()
            
            # Update save button appearance
            self.update_save_button()
            
            # Refresh the docked track info panel
            self._update_track_info()
    
    def get_song_name(self, filepath: Path) -> str:
        """Extract song name from metadata or use filename"""
        try:
            audio = mutagen.File(filepath)
            if audio and audio.tags:
                # Try different tag formats
                title = None
                if 'TIT2' in audio.tags:  # ID3
                    title = str(audio.tags['TIT2'])
                elif 'title' in audio.tags:  # Vorbis/FLAC
                    title = str(audio.tags['title'][0])
                elif '©nam' in audio.tags:  # MP4
                    title = str(audio.tags['©nam'][0])
                
                if title:
                    return title
        except:
            pass
        
        # Fallback to filename without extension
        return filepath.stem
    
    def is_song_saved(self, filepath: Path) -> bool:
        """Check if a song is marked as saved (embedded metadata tag)"""
        return read_saved_tag(filepath)
    
    def handle_volume_slider(self, value):
        """Handle volume slider value change"""
        self.audio_output.setVolume(value / 100.0)
    
    def apply_sync_config(self):
        """Push the current sync settings into the background worker"""
        if self.sync_worker is None:
            return
        self.sync_worker.update_config(
            self.sync_enabled, self.sync_url, self.sync_user,
            self.sync_auth, self.sync_key_file, self.sync_password
        )
        if self.sync_menu_action is not None:
            self.sync_menu_action.setChecked(self.sync_enabled)
    
    def toggle_remote_sync(self, checked: bool):
        """Toggle remote sync on or off (from the File menu)"""
        self.sync_enabled = checked
        self.apply_sync_config()
        self.save_config()
        self._show_status(
            "Remote sync enabled" if checked else "Remote sync disabled", 3000
        )
    
    def _show_status(self, message: str, timeout: int):
        """Show a transient message in a lazily-created status bar.

        The status bar is only created (and thus only reserves window space)
        while a message is showing, so the control buttons sit close to the
        bottom edge of the window the rest of the time. The hide timer is
        restarted on each message so rapid consecutive messages don't cut
        each other off.
        """
        bar = self.statusBar()
        bar.setStyleSheet("background-color: #1e1e1e; color: #888; font-size: 10px;")
        bar.setVisible(True)
        bar.showMessage(message, timeout)
        if self._status_hide_timer is None:
            self._status_hide_timer = QTimer(self)
            self._status_hide_timer.setSingleShot(True)
            self._status_hide_timer.timeout.connect(bar.hide)
        self._status_hide_timer.start(timeout)
    
    def on_sync_log(self, message: str):
        """Show sync progress/errors in the status bar"""
        self._show_status(message, 5000)
    
    def get_remote_name(self, filepath: Path) -> str:
        """Remote storage name for a song (its plain filename).

        The save marker is an embedded tag, so the name is stable across
        save/unsave/delete cycles.
        """
        return filepath.name
    
    def sync_upload(self, filepath: Path, remote_name: str):
        """Queue an upload of the given file to the remote server"""
        if self.sync_worker is not None:
            self.sync_worker.enqueue_upload(filepath, remote_name)
    
    def sync_remove(self, remote_name: str):
        """Queue a removal of the given remote file (if it exists)"""
        if self.sync_worker is not None:
            self.sync_worker.enqueue_remove(remote_name)
    
    def toggle_save_song(self):
        """Toggle the save status of the current song (embedded tag; the
        filename never changes)"""
        if self.current_index < 0 or self.current_index >= len(self.playlist):
            return
        
        current_file = self.playlist[self.current_index]
        
        if not current_file.exists():
            return
        
        now_saved = not self.is_song_saved(current_file)
        if not set_saved_tag(current_file, now_saved):
            self._show_status("Could not update saved status", 3000)
            return
        
        self._show_status(
            f"Song saved: {current_file.name}" if now_saved
            else f"Song un-saved: {current_file.name}", 2000
        )
        
        # Update the row's saved icon and the save button appearance
        item = self.playlist_widget.item(self.current_index, 0)
        if item:
            item.setData(Qt.ItemDataRole.UserRole + 1,
                         1 if now_saved else 0)
        self.playlist_widget.viewport().update()
        self.update_save_button()
        
        # Remote sync: upload when saving, remove when unsaving
        if now_saved:
            self.sync_upload(current_file, self.get_remote_name(current_file))
        else:
            self.sync_remove(self.get_remote_name(current_file))
    
    def update_save_button(self):
        """Update save button appearance based on current song's save status"""
        if self.current_index >= 0 and self.current_index < len(self.playlist):
            current_file = self.playlist[self.current_index]
            if self.is_song_saved(current_file):
                self.save_button.setStyleSheet(f"""
                    QPushButton {{
                        background-color: #C38C31;
                        color: #ffffff;
                        border: 1px solid #3d3d3d;
                        border-radius: 10px;
                        {BUTTON_FONT_SIZE}
                        font-weight: bold;
                    }}
                    QPushButton:hover {{
                        background-color: #ECAA40;
                        border: 1px solid #4d4d4d;
                    }}
                    QPushButton:pressed {{
                        background-color: #ECAA40;
                    }}
                """)
            else:
                # Reset to default style
                self.save_button.setStyleSheet(BUTTON_FONT_SIZE)
    
    def reset_delete_state(self):
        """Reset delete button click state"""
        self.delete_click_count = 0
        self.delete_last_click_time = 0
        self.delete_last_song_index = -1
    
    def handle_delete_click(self):
        """Handle delete button click with double-click detection"""
        current_time = time.time()
        
        # If song changed, reset state
        if self.delete_last_song_index != self.current_index:
            self.reset_delete_state()
            self.delete_last_song_index = self.current_index
        
        # Check if this is within 1 second of last click
        if current_time - self.delete_last_click_time <= 1.0:
            # Second click within time window - delete the song
            self.delete_click_count += 1
            if self.delete_click_count >= 2:
                self.delete_current_song()
                self.reset_delete_state()
        else:
            # First click or too much time passed
            self.delete_click_count = 1
            self.delete_last_click_time = current_time
    
    def delete_current_song(self):
        """Delete the current song file and move to next"""
        if self.current_index < 0 or self.current_index >= len(self.playlist):
            return
        
        current_file = self.playlist[self.current_index]
        
        if not current_file.exists():
            return
        
        # Prevent deletion of saved songs
        if self.is_song_saved(current_file):
            print(f"Cannot delete saved song: {current_file.name}")
            return
        
        try:
            # Stop playback
            self.player.stop()
            
            # Delete the file
            current_file.unlink()
            
            # Remote sync: remove from remote server if it exists
            self.sync_remove(self.get_remote_name(current_file))
            
            # Remove from playlist
            del self.playlist[self.current_index]
            self.playlist_widget.removeRow(self.current_index)
            
            # Move to next song or stop if no more songs
            if self.playlist:
                # Determine next index based on play order
                if self.play_order == PlayOrder.OLDEST_TO_NEWEST:
                    # Playing bottom-to-top (increasing indices)
                    # After deletion, current_index now points to what was the next song
                    # If we deleted the last song, wrap to beginning
                    if self.current_index >= len(self.playlist):
                        self.current_index = 0
                    # Otherwise current_index is already pointing at the next song
                else:  # NEWEST_TO_OLDEST
                    # Playing top-to-bottom (decreasing indices)
                    # After deletion at position N, the song that was at N+1 is now at N
                    # We want to continue downward, so stay at current_index
                    # But if we deleted at the bottom, go to top
                    if self.current_index >= len(self.playlist):
                        self.current_index = 0
                
                # Load and play next song
                self.load_current_song()
                self.player.play()
                self.play_pause_button.setText("⏸")
            else:
                # No more songs
                self.current_index = -1
                self.song_label.setText("No song loaded")
                self.play_pause_button.setText("▶")
                self._update_track_info()
            
        except Exception as e:
            print(f"Error deleting file: {e}")
    
    def toggle_play_pause(self):
        """Toggle between play and pause"""
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_pause_button.setText("▶")
        else:
            if self.current_index == -1 and self.playlist:
                self.current_index = self.get_starting_index()
                self.load_current_song()
            self.player.play()
            self.play_pause_button.setText("⏸")
    
    def next_song(self):
        """Skip to next song"""
        if not self.playlist:
            return
        
        if self.loop_mode == LoopMode.LOOP_SINGLE:
            self.player.setPosition(0)
            self.player.play()
        else:
            self.current_index = self.get_next_index()
            self.load_current_song()
            self.player.play()
            self.play_pause_button.setText("⏸")
        
        # Reset delete state when changing songs
        self.reset_delete_state()
    
    def previous_song(self):
        """Go to previous song"""
        if not self.playlist:
            return
        
        # If more than 3 seconds into song, restart it
        if self.player.position() > 3000:
            self.player.setPosition(0)
        else:
            self.current_index = self.get_previous_index()
            self.load_current_song()
            self.player.play()
            self.play_pause_button.setText("⏸")
        
        # Reset delete state when changing songs
        self.reset_delete_state()
    
    def toggle_loop_mode(self):
        """Cycle through loop modes"""
        modes = list(LoopMode)
        current_idx = modes.index(self.loop_mode)
        self.loop_mode = modes[(current_idx + 1) % len(modes)]
        self.update_loop_button()
        self.save_config()
    
    def update_loop_button(self):
        """Update loop button appearance"""
        if self.loop_mode == LoopMode.NO_LOOP:
            self.loop_button.setText("↻")
            self.loop_button.setStyleSheet(f"QPushButton {{ color: #888; {BUTTON_FONT_SIZE} }}")
        elif self.loop_mode == LoopMode.LOOP_PLAYLIST:
            self.loop_button.setText("🔁")
            self.loop_button.setStyleSheet(f"QPushButton {{ color: #4d9eff; {BUTTON_FONT_SIZE} }}")
        else:  # LOOP_SINGLE
            self.loop_button.setText("🔂")
            self.loop_button.setStyleSheet(f"QPushButton {{ color: #4d9eff; {BUTTON_FONT_SIZE} }}")
    
    def seek(self, position):
        """Seek to position in current song"""
        self.player.setPosition(position)
    
    def update_position(self, position):
        """Update position display"""
        self.seek_slider.setValue(position)
        self.time_label.setText(self.format_time(position))
    
    def update_duration(self, duration):
        """Update duration display"""
        self.seek_slider.setRange(0, duration)
        self.duration_label.setText(self.format_time(duration))
    
    def format_time(self, ms: int) -> str:
        """Format milliseconds to MM:SS"""
        seconds = ms // 1000
        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes}:{seconds:02d}"
    
    def on_media_status_changed(self, status):
        """Handle media status changes"""
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.loop_mode == LoopMode.LOOP_SINGLE:
                self.player.setPosition(0)
                self.player.play()
            elif self.loop_mode == LoopMode.LOOP_PLAYLIST:
                self.next_song()
            else:
                # No loop - check if we should continue based on play order
                if self.play_order == PlayOrder.OLDEST_TO_NEWEST:
                    # Playing oldest to newest (bottom to top)
                    # Continue if not at top (index 0)
                    if self.current_index > 0:
                        self.next_song()
                    else:
                        self.play_pause_button.setText("▶")
                else:  # NEWEST_TO_OLDEST
                    # Playing newest to oldest (top to bottom)
                    # Continue if not at bottom (last index)
                    if self.current_index < len(self.playlist) - 1:
                        self.next_song()
                    else:
                        self.play_pause_button.setText("▶")
    
    def load_config(self):
        """Load configuration from file"""
        if not self.CONFIG_FILE.exists():
            return
        
        try:
            with open(self.CONFIG_FILE, 'r') as f:
                config = json.load(f)
            
            if 'target_directory' in config:
                directory = Path(config['target_directory'])
                if directory.exists():
                    self.set_target_directory(directory)
            
            if 'loop_mode' in config:
                self.loop_mode = LoopMode(config['loop_mode'])
                self.update_loop_button()
            
            if 'play_order' in config:
                self.play_order = PlayOrder(config['play_order'])
            
            if 'volume' in config:
                self.audio_output.setVolume(config['volume'])
                self.volume_slider.setValue(int(config['volume'] * 100))
            
            if 'initial_songs_count' in config:
                self.initial_songs_count = config['initial_songs_count']
            
            if 'sync_enabled' in config:
                self.sync_enabled = bool(config['sync_enabled'])
            
            if 'sync_url' in config:
                self.sync_url = config['sync_url']
            
            if 'sync_user' in config:
                self.sync_user = config['sync_user']
            
            if 'sync_auth' in config:
                self.sync_auth = config['sync_auth']
            
            if 'sync_key_file' in config:
                self.sync_key_file = config['sync_key_file']
            
            if 'sync_password' in config:
                self.sync_password = decrypt_secret(config['sync_password'])
                
        except Exception as e:
            print(f"Error loading config: {e}")
    
    def save_config(self):
        """Save configuration to file"""
        self.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        
        config = {
            'loop_mode': self.loop_mode.value,
            'play_order': self.play_order.value,
            'volume': self.audio_output.volume(),
            'initial_songs_count': self.initial_songs_count,
            'sync_enabled': self.sync_enabled,
            'sync_url': self.sync_url,
            'sync_user': self.sync_user,
            'sync_auth': self.sync_auth,
            'sync_key_file': self.sync_key_file,
            'sync_password': encrypt_secret(self.sync_password)
        }
        
        if self.target_directory:
            config['target_directory'] = str(self.target_directory)
        
        try:
            with open(self.CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            print(f"Error saving config: {e}")
    
    def closeEvent(self, event):
        """Handle application close"""
        # Stop playback immediately
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        
        # Disconnect all signals to prevent callbacks during shutdown
        try:
            self.player.positionChanged.disconnect()
            self.player.durationChanged.disconnect()
            self.player.mediaStatusChanged.disconnect()
        except:
            pass
        
        # Stop and clear player
        self.player.stop()
        self.player.setSource(QUrl())
        
        # Save config
        try:
            self.save_config()
        except:
            pass
        
        # Stop file watcher in a non-blocking way
        if self.watcher_thread and self.watcher_thread.isRunning():
            self.watcher_thread.quit()
            # Don't wait indefinitely - give it 500ms max
            self.watcher_thread.wait(500)
        
        # Stop the background sync worker
        if self.sync_worker:
            self.sync_worker.stop()
        
        event.accept()


def main():
    global BitmapFontFamily
    app = QApplication(sys.argv)
    app.setApplicationName("Musibisk")
    bitmap_font_id = QFontDatabase.addApplicationFontFromData(QByteArray(base64.b64decode(BITMAP_FONT)))
    BitmapFontFamily = QFontDatabase.applicationFontFamilies(bitmap_font_id)[0]
    icon = icon_from_base64_png(ICON_PNG_BASE64)
    app.setWindowIcon(icon)
    
    window = Musibisk()
    window.show()
    
    sys.exit(app.exec())


if __name__ == '__main__':
    main()