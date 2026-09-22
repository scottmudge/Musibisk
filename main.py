#!/usr/bin/env python3
"""
Musibisk - A sleek, minimal music player with directory monitoring
"""

import sys
import json
import os
import re
import struct
import queue
import threading
from pathlib import Path
from typing import List, Optional
from enum import Enum
import base64
import time
from datetime import datetime
from urllib.parse import urlsplit

try:
    import numpy as np  # FFT for the spectrum visualizer (pip install numpy)
except ImportError:
    np = None

try:
    import av  # PyAV: in-process FFmpeg decode, no subprocess (pip install av)
except ImportError:
    av = None

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSlider, QFileDialog, QMenuBar, QMenu,
    QListWidget, QListWidgetItem, QDialog, QFormLayout, QSpinBox,
    QDialogButtonBox, QFrame, QDial, QTableWidget, QTableWidgetItem,
    QHeaderView, QComboBox, QStyledItemDelegate, QStyleOptionViewItem,
    QStyle, QCheckBox, QLineEdit, QToolButton
)
from PyQt6.QtCore import (
    Qt, QTimer, QUrl, QThread, pyqtSignal, pyqtProperty, QObject,
    QByteArray,
    QModelIndex, QRect, QElapsedTimer, QPointF, QRectF, QEvent,
    QPropertyAnimation, QEasingCurve
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtGui import (
    QAction, QKeySequence, QIcon, QPixmap, QMouseEvent, QFont, QFontDatabase,
    QColor, QImage, QPainter, QPainterPath, QPolygonF, QPen,
    QGuiApplication
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


def compute_waveform_peaks(filepath, cols_per_second=120):
    """Compute a normalized amplitude profile (values in (0, 1]) for a file,
    one value per column of a WaveformVisualizer.

    Decodes the track once, in-process, with PyAV (mono float32, 10 samples
    per column) when available; falls back to a content-energy heuristic on
    the raw bytes so the visualizer still works without PyAV. Returns [] on
    failure.
    """
    decode_rate = cols_per_second * 10
    data = None
    if av is not None:
        container = None
        try:
            container = av.open(str(filepath))
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(
                format='flt', layout='mono', rate=decode_rate)
            chunks = []
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().ravel().tobytes())
            data = b''.join(chunks)
        except Exception:
            data = None
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass

    if data and len(data) >= 4:
        per = max(1, decode_rate // cols_per_second)
        if np is not None:
            samples = np.frombuffer(data, dtype='<f4')
            n_full = len(samples) - (len(samples) % per)
            peaks = [float(t) for t in
                     np.abs(samples[:n_full].reshape(-1, per)).max(axis=1)]
        else:
            samples = struct.unpack(f'<{len(data) // 4}f', data)
            peaks = []
            for i in range(0, len(samples) - (len(samples) % per), per):
                top = 0.0
                for v in samples[i:i + per]:
                    magn = -v if v < 0 else v
                    if magn > top:
                        top = magn
                peaks.append(top)
        if peaks:
            scale = max(peaks)
            if scale > 0:
                peaks = [min(1.0, (p / scale) ** 0.7) for p in peaks]
            return peaks

    # Fallback: deterministic content-energy profile from the raw bytes
    try:
        size = Path(filepath).stat().st_size
        n_cols = 2048
        step = max(1, size // n_cols)
        peaks = []
        with open(filepath, 'rb') as f:
            for c in range(n_cols):
                f.seek(min(c * step, max(0, size - 1)))
                chunk = f.read(512)
                if not chunk:
                    peaks.append(0.0)
                    continue
                e = 0
                for b in chunk:
                    d = b - 128
                    e += -d if d < 0 else d
                peaks.append(e / len(chunk) / 128.0)
        scale = max(peaks) if peaks else 0.0
        if scale > 0:
            peaks = [min(1.0, (p / scale) ** 0.7) for p in peaks]
        return peaks
    except Exception:
        return []


class _AudioTapReader:
    """In-process audio tap: decode the track with PyAV (FFmpeg compiled
    into a shared library — no subprocess) in a worker thread and pump raw
    PCM (44.1 kHz stereo float32) into a bounded in-memory buffer.

    The visualizer pulls (drains) bytes off the front in realtime-paced
    amounts, so the displayed audio matches the player's clock. Decoding
    runs ~1000x faster than realtime, so the buffer (a few seconds of
    head-room) fills instantly and the thread then blocks until the
    consumer drains it — decode CPU is a one-shot burst per
    play/seek and zero otherwise. The decoder only exists while audio is
    playing: pausing stops it (zero decode CPU while paused) and
    resume/seek restarts it at the right offset.
    """
    _CAP_BYTES = 4 * 1024 * 1024  # ~45 s of f32le stereo (headroom, not latency)
    _SR = 44100

    def __init__(self, parent=None):
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._thread = None
        self._running = False
        self._gen = 0

    # ------------------------------------------------------------ lifecycle
    def open_track(self, path, offset_ms):
        """(Re)start decoding `path` from `offset_ms` (main thread)."""
        with self._cond:
            # retire any previous decode generation first
            self._running = False
            self._cond.notify_all()
            del self._buf[:]
            self._gen += 1
            self._running = True
        thread = threading.Thread(
            target=self._run, args=(self._gen, str(path),
                                    float(max(0.0, offset_ms))),
            daemon=True)
        prev = self._thread
        self._thread = thread
        thread.start()
        if prev is not None and prev is not threading.current_thread():
            prev.join(1.5)  # keeps the shared buffer single-writer

    def close_track(self):
        """Stop the decoder (if any) and clear the buffer (main thread)."""
        with self._cond:
            self._running = False
            self._cond.notify_all()
            del self._buf[:]
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.5)

    def alive(self):
        """True while a decode generation is still running.

        False means either "never opened / closed" or "open failed"
        (unsupported file, missing av) — the visualizer treats a dead tap
        as disabled.
        """
        return (self._running and self._thread is not None
                and self._thread.is_alive())

    # -------------------------------------------------------------- decode
    def _run(self, gen, path, offset_ms):
        """Decode loop (worker thread). Writes only while `gen` is current,
        so a superseded generation can never corrupt a new tap."""
        container = None
        ok = False
        if av is not None:
            try:
                container = av.open(path)
                stream = container.streams.audio[0]
                resampler = av.AudioResampler(
                    format='flt', layout='stereo', rate=self._SR)
                offset_us = int(offset_ms * 1000)
                if offset_us > 0:
                    # backward=True: land on the last keyframe AT/BEFORE the
                    # target; pre-target frames are skipped below
                    container.seek(offset_us, any_frame=False,
                                   backward=True, stream=stream)
                skip = offset_us > 0
                for frame in container.decode(stream):
                    if gen != self._gen:
                        break
                    if skip:
                        if frame.pts is None:
                            skip = False  # untimestamped: assume in place
                        else:
                            pts_us = int(round(
                                float(frame.pts * stream.time_base) * 1e6))
                            if pts_us < offset_us:
                                continue
                            skip = False
                    for out in resampler.resample(frame):
                        data = out.to_ndarray().ravel().tobytes()
                        with self._cond:
                            if gen != self._gen:
                                break
                            # bounded queue: if the cap is hit, block until
                            # the consumer drains some (decoding is ~1000x
                            # realtime, so this is the normal state)
                            while (len(self._buf) + len(data)
                                   > self._CAP_BYTES
                                   and self._running
                                   and gen == self._gen):
                                self._cond.wait(0.25)
                            if gen != self._gen:
                                break
                            self._buf.extend(data)
                            self._cond.notify()
                ok = True
            except Exception:
                ok = False
            finally:
                if container is not None:
                    try:
                        container.close()
                    except Exception:
                        pass
        if not ok:
            # open/decode failed: report death so the tap is disabled
            with self._cond:
                if gen == self._gen:
                    self._running = False
                    self._cond.notify_all()
        else:
            # decode finished (EOF or stop): keep the thread alive until
            # closed so a fully-prefilled buffer can still be drained
            # (short tracks fit entirely inside the cap)
            with self._cond:
                while self._running and gen == self._gen:
                    self._cond.wait(0.25)

    # --------------------------------------------------------------- buffer
    def pending(self) -> int:
        with self._cond:
            return len(self._buf)

    def drain(self, n: int) -> bytes:
        """Pull up to n bytes off the front (main thread)."""
        with self._cond:
            n = min(n, len(self._buf))
            if n <= 0:
                return b''
            out = bytes(self._buf[:n])
            del self._buf[:n]
            self._cond.notify()
        return out


class _WaveformWorker(QThread):
    """Decode one track's amplitude profile in the background."""
    done = pyqtSignal(int, list)  # (generation, peaks)

    def __init__(self, generation, filepath):
        super().__init__()
        self._generation = generation
        self._filepath = filepath

    def run(self):
        peaks = compute_waveform_peaks(self._filepath)
        self.done.emit(self._generation, peaks)


class WaveformVisualizer(QWidget):
    """Realtime visualizer box for the playing track.

    Four visualizers share the same 260x44 box; click the box to cycle:
      waveform      - the track's full amplitude profile, scrolling
      vu            - two VU bars (left/right; identical for mono)
      spectrum      - frequency-bin intensity bars (gold, 1px continuous)
      oscilloscope  - the current audio waveform, like a real scope

    Low-resource design:
      * waveform: the amplitude profile is decoded ONCE in a background
        thread and cached; each frame draws only the ~260 visible columns
        as antialiased polygons with sub-pixel vertices (smooth scroll).
      * vu/spectrum/scope: the track is decoded IN-PROCESS with PyAV
        (FFmpeg as a shared library — no subprocess) in a worker thread
        that pumps raw PCM into a bounded buffer; the main thread consumes
        it in realtime paced amounts (a few KB per frame). The spectrum is
        a 4096-point numpy FFT per frame (~tens of microseconds);
        everything else is small vector ops. The decoder is STOPPED while
        paused (zero decode CPU) and restarted on resume/seek at the right
        offset.
    All drawing runs on a drift-corrected 60 FPS grid via a 2 ms timer
    that idles when nothing is animating.

    The waveform playhead sits at the horizontal middle; the position is a
    local time line (anchored to the first player report, advanced by the
    real clock) plus a persistent low-passed correction for the player's
    coarse, slightly late reports — constant between reports, so the
    scroll speed never pulses.
    """
    HEIGHT = 44
    #     Tunable knob — sets BOTH the horizontal resolution and the scroll
    # speed: N waveform columns per second of audio, i.e. the display
    # scrolls at N pixels/second and shows (width / N) seconds of context
    # (half behind, half ahead of the centered playhead).
    #   60  -> slow, ~4.3 s visible    120 -> 2x (default)    240 -> 4x
    COLS_PER_SECOND = 120
    PLAYHEAD_RATIO = 0.5  # playhead ("now" line) at the horizontal middle

    MODES = ('waveform', 'vu', 'spectrum', 'oscilloscope')
    MODE_LABELS = {'waveform': 'WAVE', 'vu': 'VU',
                   'spectrum': 'SPEC', 'oscilloscope': 'SCOPE'}

    BG_COLOR = QColor('#1b1b1b')
    BORDER_COLOR = QColor('#3d3d3d')
    ACCENT_COLOR = QColor('#ffd700')
    DIM_COLOR = QColor('#555555')
    PLACEHOLDER_COLOR = QColor('#333333')
    PLAYHEAD_COLOR = QColor('#ffffff')
    LABEL_COLOR = QColor('#666666')

    _ALPHA = 0.25            # per-report low-pass factor on the clock offset
    _SNAP_THRESHOLD_MS = 1500.0  # larger discontinuities (seeks) snap
    _PAUSED_STALE_MS = 1000.0    # backward reports this close while paused
    # are stale samples from the just-paused buffer and are ignored
    _FRAME_PERIOD = 1000.0 / 60.0  # 60 FPS drawing grid
    _TIMER_MS = 2            # wake frequently enough to hit the grid on time

    # Audio tap / derived-data parameters (44.1 kHz f32le stereo = 352 800
    # bytes/s; one frame is 8 bytes)
    _SR = 44100
    _RATE_BPS = 44100 * 2 * 4
    _RING_FRAMES = 8192      # ring buffer of decoded frames (4096/side)
    _SPEC_BINS = 96          # spectrum band count (rendered 1px/col)
    _SPEC_FRAME = 4096       # FFT size
    _SPEC_F0 = 40.0          # lowest band frequency (Hz)
    _SPEC_F1 = 16000.0       # highest band frequency (Hz)
    _SPEC_DB_FLOOR = -60.0   # bar height 0.0 at this band RMS level
    _SPEC_DB_TOP = -6.0      # bar height 1.0 at this band RMS level
    _SCOPE_N = 220           # oscilloscope samples shown
    _VU_DECAY = 0.88         # per-frame VU release (60 fps)
    _SPEC_DECAY = 0.90       # per-frame spectrum release while paused

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if av is None:
            self.setToolTip("Live VU / spectrum / oscilloscope need the\n"
                            "'av' package (pip install av).\n"
                            "Click to switch visualizer")
        else:
            self.setToolTip("Click to switch visualizer "
                            "(waveform / VU / spectrum / oscilloscope)")
        self._generation = 0
        self._track_path = None
        self._peaks: List[float] = []
        self._worker = None
        self._playing = False
        self._anchor_pos = 0.0    # ms, base position
        self._anchor_time = 0.0   # clock ms of the anchor
        self._offset_init = False  # no report anchored the line yet
        self._offset = 0.0        # ms, persistent low-passed clock correction
        self._next_frame = 0.0    # clock ms of the next scheduled draw
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(self._TIMER_MS)
        self._timer.timeout.connect(self._tick)
        self._clock.start()

        # -- visualizer mode --
        self._mode = 'waveform'
        self._label_font = QFont()
        self._label_font.setPixelSize(8)

        # -- audio tap (live PCM) and derived data --
        self._tap = _AudioTapReader(self)
        self._tap_path: Optional[Path] = None
        self._tap_active = False          # in-process decoder running
        self._tap_offset_ms = 0.0         # file offset (ms) of the anchor
        self._tap_anchor_time = 0.0       # clock ms of the tap anchor
        if np is not None:
            self._ring_l = np.zeros(self._RING_FRAMES, dtype=np.float32)
            self._ring_r = np.zeros(self._RING_FRAMES, dtype=np.float32)
            self._spec = np.zeros(self._SPEC_BINS, dtype=np.float32)
            self._spec_hann = np.hanning(self._SPEC_FRAME)
            self._spec_hann_sum = float(self._spec_hann.sum())
            edges = np.logspace(np.log10(self._SPEC_F0),
                                np.log10(self._SPEC_F1),
                                self._SPEC_BINS + 1)
            e = np.clip((edges * self._SPEC_FRAME / self._SR).astype(int),
                        0, self._SPEC_FRAME // 2)
            # low-frequency bands are narrower than one FFT bin; force
            # strictly increasing edges so every band gets >= 1 FFT bin
            for i in range(1, len(e)):
                if e[i] <= e[i - 1]:
                    e[i] = e[i - 1] + 1
            self._spec_edges = e.tolist()
        else:
            self._ring_l = None
            self._ring_r = None
            self._spec = None
            self._spec_hann = None
            self._spec_edges = None
        self._samp_pos = 0
        self._samp_count = 0
        self._frames_consumed = 0  # total PCM frames consumed since reset
        self._vu_l = 0.0
        self._vu_r = 0.0
        self._vu_l_target = 0.0
        self._vu_r_target = 0.0

    # ---------------------------------------------------------------- API
    def set_track(self, filepath):
        """Start (or keep) showing the visualizer for this file."""
        filepath = Path(filepath)
        if filepath == self._track_path:
            return
        self._track_path = filepath
        self._generation += 1
        self._peaks = []
        self._anchor_pos = 0.0
        self._anchor_time = self._clock.elapsed()
        self._offset_init = False
        self._offset = 0.0
        if self._worker is not None:
            self._worker.wait(2000)
        self._worker = _WaveformWorker(self._generation, filepath)
        self._worker.done.connect(self._on_peaks_ready)
        self._worker.start()
        self.set_audio_track(filepath)
        self.update()

    def clear(self):
        """Reset to the no-track placeholder state."""
        self._track_path = None
        self._generation += 1
        self._peaks = []
        self._playing = False
        self.stop_audio_tap()
        self._timer.stop()
        self.update()

    def set_playing(self, playing: bool):
        """Run/pause the animation with the playback state."""
        if playing == self._playing:
            return
        if playing:
            self._playing = True
            if not self._offset_init:
                # fresh track: the position starts at zero NOW (the anchor
                # set in set_track may predate the actual playback start)
                self._anchor_pos = 0.0
                self._anchor_time = self._clock.elapsed()
            self._next_frame = self._clock.elapsed() + self._FRAME_PERIOD
        else:
            # Pause: FREEZE — fold the elapsed time (and correction) into
            # the anchor so the displayed position stays exactly where it
            # is, and resuming continues from there without a jump.
            self._anchor_pos = self._current_position()
            self._anchor_time = self._clock.elapsed()
            self._offset = 0.0
            self._playing = False
        self.set_audio_playing(playing)
        self._timer_policy()
        self.update()

    # -------------------------------------------------- visualizer mode API
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str):
        """Switch the visualizer (from the config file at startup)."""
        if mode in self.MODES:
            self._mode = mode
            self.update()

    def cycle_mode(self):
        """Advance to the next visualizer (called on click)."""
        self._mode = self.MODES[(self.MODES.index(self._mode) + 1)
                                % len(self.MODES)]
        self.update()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.cycle_mode()
        super().mousePressEvent(event)

    # ---------------------------------------------------------- audio tap API
    def _tap_enabled(self, path: Optional[Path] = None) -> bool:
        path = path if path is not None else self._tap_path
        return (np is not None
                and av is not None
                and path is not None
                and Path(path).exists())

    def set_audio_track(self, filepath):
        """Remember the track for the live PCM tap (position 0).

        The in-process decoder itself only runs while audio is actually
        playing (see set_audio_playing) — zero decode CPU otherwise.
        """
        filepath = Path(filepath)
        if filepath == self._tap_path:
            return
        self._reset_audio_state()
        self._tap_path = filepath

    def set_audio_playing(self, playing: bool):
        """Keep the in-process decoder in step with the player's play/pause.

        Pausing STOPS the decoder (no decode CPU while paused); the file
        position is remembered and resume restarts the decoder there.
        """
        if not self._tap_enabled():
            return
        if playing:
            if not self._tap_active:
                self._start_tap_at(self._tap_path,
                                   self._tap_file_position_ms())
            else:
                # The decoder has been head-decoding since playback
                # (re)started: re-anchor the pacing to NOW so the tap
                # follows the player's (re)start, not the earlier one.
                self._tap_anchor_time = self._clock.elapsed()
        elif self._tap_active:
            self._tap_offset_ms = self._tap_file_position_ms()
            self._tap.close_track()
            self._tap_active = False

    def seek_audio(self, pos_ms: int):
        """Follow a player seek so the tap stays on the right audio."""
        if not self._tap_enabled():
            return
        if self._tap_active:
            self._start_tap_at(self._tap_path, int(pos_ms))
        else:
            # paused: remember where to resume from
            self._tap_offset_ms = float(max(0, pos_ms))

    def stop_audio_tap(self):
        """Stop the tap and forget the file (no-track state)."""
        self._reset_audio_state()
        self._tap_path = None
        if self._tap_active:
            self._tap.close_track()
            self._tap_active = False

    def _start_tap_at(self, path, offset_ms: float):
        self._tap.open_track(path, offset_ms)
        self._tap_offset_ms = float(offset_ms)
        self._tap_anchor_time = self._clock.elapsed()
        self._tap_active = True

    def _reset_audio_state(self):
        if self._tap_active:
            self._tap.close_track()
            self._tap_active = False
        self._tap_offset_ms = 0.0
        self._tap_anchor_time = self._clock.elapsed()
        self._samp_pos = 0
        self._samp_count = 0
        self._frames_consumed = 0
        self._vu_l = 0.0
        self._vu_r = 0.0
        self._vu_l_target = 0.0
        self._vu_r_target = 0.0
        if self._ring_l is not None:
            self._ring_l.fill(0)
            self._ring_r.fill(0)
        if self._spec is not None:
            self._spec.fill(0)

    def _tap_file_position_ms(self) -> float:
        """Where the tap currently sits in the file (ms)."""
        if not self._tap_active:
            return self._tap_offset_ms
        elapsed_s = (self._clock.elapsed() - self._tap_anchor_time) / 1000.0
        return self._tap_offset_ms + elapsed_s * 1000.0

    def feed_samples(self, samples):
        """Ingest stereo float32 PCM of shape (n, 2) — the tap calls this,
        and tests may call it directly with synthetic audio."""
        if self._ring_l is None or samples is None or len(samples) == 0:
            return
        n = len(samples)
        if n >= self._RING_FRAMES:
            samples = samples[-self._RING_FRAMES:]
            n = self._RING_FRAMES
        l = samples[:, 0]
        r = samples[:, 1]
        pos = self._samp_pos
        size = self._RING_FRAMES
        if pos + n > size:
            first = size - pos
            self._ring_l[pos:] = l[:first]
            self._ring_r[pos:] = r[:first]
            self._ring_l[:n - first] = l[first:]
            self._ring_r[:n - first] = r[first:]
        else:
            self._ring_l[pos:pos + n] = l
            self._ring_r[pos:pos + n] = r
        self._samp_pos = (pos + n) % size
        self._samp_count = min(self._samp_count + n, size)
        # VU: per-chunk peaks (fast attack happens on feed)
        self._vu_l_target = max(self._vu_l_target,
                                float(np.max(np.abs(l))))
        self._vu_r_target = max(self._vu_r_target,
                                float(np.max(np.abs(r))))

    def note_position(self, pos_ms: int):
        """Feed a player position report (from positionChanged).

        The player reports coarsely and slightly late. The first report
        anchors the local time line; afterwards a report's deviation from
        the line is a low-passed correction to a PERSISTENT offset, so
        the scroll speed stays perfectly constant between reports (a
        transient ease-out per report would pulse with the report rate).
        Big discontinuities (seeks) snap immediately.
        """
        now = self._clock.elapsed()
        if self._playing:
            base = self._anchor_pos + (now - self._anchor_time)
            dev = float(pos_ms) - base
            if not self._offset_init:
                self._anchor_pos = float(pos_ms)
                self._anchor_time = now
                self._offset = 0.0
                self._offset_init = True
            elif abs(dev - self._offset) > self._SNAP_THRESHOLD_MS:
                self._anchor_pos = float(pos_ms)
                self._anchor_time = now
                self._offset = 0.0
                self.seek_audio(pos_ms)  # resync the audio tap to the seek
            else:
                self._offset += (dev - self._offset) * self._ALPHA
        else:
            # Paused: adopt forward reports (buffer drain / seek forward)
            # and well-behind reports (a real seek back); a report that is
            # slightly behind is a stale sample from the just-paused buffer
            # and is ignored so the frozen position holds.
            cur = self._current_position()
            if float(pos_ms) > cur or cur - float(pos_ms) > self._PAUSED_STALE_MS:
                self._anchor_pos = float(pos_ms)
                self._anchor_time = now
                self._offset = 0.0
                self._offset_init = True
                self.seek_audio(pos_ms)
        if not self._playing or not self._timer.isActive():
            self.update()

    # ---------------------------------------------------------- internals
    def _on_peaks_ready(self, generation, peaks):
        if generation != self._generation or not peaks:
            return
        self._peaks = peaks
        if self._playing:
            self._next_frame = self._clock.elapsed() + self._FRAME_PERIOD
        self._timer_policy()
        self.update()

    def _current_position(self):
        """Playback position in ms: the anchored time line (linear between
        the player's coarse reports) plus the persistent clock correction."""
        pos = self._anchor_pos
        if self._playing:
            pos += self._clock.elapsed() - self._anchor_time
        pos += self._offset
        return max(0.0, pos)

    def _timer_policy(self):
        """The 60 FPS timer runs only while something is animating:
        playback (waveform scroll / live audio) or VU/spectrum decays
        still settling after a pause."""
        needed = self._playing
        if not needed and self._spec is not None:
            needed = (self._vu_l > 0.002 or self._vu_r > 0.002
                      or bool(self._spec.max() > 0.002))
        if needed and not self._timer.isActive():
            self._next_frame = self._clock.elapsed() + self._FRAME_PERIOD
            self._timer.start()
        elif not needed and self._timer.isActive():
            self._timer.stop()

    def _tick(self):
        # Draw on a drift-corrected 60 FPS grid: uniform frame spacing
        # keeps the perceived scroll speed constant (a plain 16 ms timer
        # drifts against the display refresh and reads as speed pulsing).
        now = self._clock.elapsed()
        if now < self._next_frame:
            return
        if now - self._next_frame > 30.0:
            self._next_frame = now + self._FRAME_PERIOD
        else:
            self._next_frame += self._FRAME_PERIOD
        self._frame_update()
        self.update()

    def _frame_update(self):
        """Per-frame (60 FPS) state work: paced tap consumption, VU
        release, spectrum update/decay. All numpy ops are tiny (a 4096
        point FFT plus a few vector passes)."""
        # 1) pull decoded PCM up to the present (realtime paced)
        if self._tap_active and self._playing and self._tap_path is not None:
            if not self._tap.alive():
                # the decoder died (open/decode error): forget it
                self._tap_active = False
            else:
                now = self._clock.elapsed()
                due = ((now - self._tap_anchor_time) / 1000.0
                       * self._RATE_BPS)
                # frame-aligned: one stereo float32 frame is 8 bytes
                n = (min(int(due), self._tap.pending()) // 8) * 8
                if n > 0:
                    data = self._tap.drain(n)
                    samples = np.frombuffer(data, dtype=np.float32)
                    samples = np.ascontiguousarray(samples.reshape(-1, 2))
                    self.feed_samples(samples)
                    self._frames_consumed += len(samples)
                    self._tap_offset_ms += len(data) / self._RATE_BPS * 1000.0
                    self._tap_anchor_time = now

        # 2) VU release (fast attack already applied on feed)
        self._vu_l = max(self._vu_l_target, self._vu_l * self._VU_DECAY)
        self._vu_r = max(self._vu_r_target, self._vu_r * self._VU_DECAY)
        self._vu_l_target = 0.0
        self._vu_r_target = 0.0

        # 3) spectrum: live while playing, decaying otherwise
        if self._spec is not None:
            if self._playing and self._samp_count >= self._SPEC_FRAME:
                self._update_spectrum()
            elif not self._playing and bool(self._spec.max() > 0.0):
                self._spec *= self._SPEC_DECAY

        self._timer_policy()

    def _update_spectrum(self):
        """Band RMS levels (dBFS) of the last 4096 samples, with fast
        attack / slow release smoothing.

        RMS (not max) is what a real analyzer shows: max-pooling a 93 ms
        window picks up every transient, which pins nearly every band at
        full scale for music. Each log band's power is the MEAN of its
        FFT bins' power, mapped on a -60..-6 dBFS scale.
        """
        mono = self._ring_mono(self._SPEC_FRAME)
        if mono is None:
            return
        mag = np.abs(np.fft.rfft(mono * self._spec_hann))
        mag *= 2.0 / self._spec_hann_sum  # Hann amplitude normalization
        power = mag * mag
        starts = self._spec_edges[:-1]
        ends = self._spec_edges[1:-1] + [power.size]
        counts = np.maximum(1, np.asarray(ends) - np.asarray(starts))
        band_power = np.add.reduceat(power, starts) / counts
        db = 20.0 * np.log10(np.sqrt(band_power) + 1e-9)
        target = np.clip(
            (db - self._SPEC_DB_FLOOR)
            / (self._SPEC_DB_TOP - self._SPEC_DB_FLOOR), 0.0, 1.0
        ).astype(np.float32)
        prev = self._spec
        self._spec = np.where(
            target > prev,
            prev + (target - prev) * 0.5,   # fast attack
            prev + (target - prev) * 0.15)  # slow release

    def _ring_slice(self, n: int):
        """The last n frames (l, r) from the ring, oldest first."""
        size = self._RING_FRAMES
        n = min(n, self._samp_count, size)
        if n <= 0:
            return None
        start = (self._samp_pos - n) % size
        if start + n <= size:
            l = self._ring_l[start:start + n]
            r = self._ring_r[start:start + n]
        else:
            first = size - start
            l = np.concatenate((self._ring_l[start:],
                                self._ring_l[:n - first]))
            r = np.concatenate((self._ring_r[start:],
                                self._ring_r[:n - first]))
        return l, r

    def _ring_mono(self, n: int):
        sl = self._ring_slice(n)
        if sl is None:
            return None
        return (sl[0] + sl[1]) * 0.5

    def scope_array(self):
        """The last _SCOPE_N mono samples (for the oscilloscope), or None."""
        return self._ring_mono(self._SCOPE_N)

    def _band(self, painter, c_start, c_end, pf, phx, mid, scale, color):
        """Draw one played/upcoming band as a mirrored antialiased polygon.
        Vertices sit at sub-pixel x positions for smooth scrolling."""
        if c_end < c_start:
            return
        cols = []
        peaks = self._peaks
        for c in range(c_start, c_end + 1):
            a = peaks[c] * scale
            if a < 0.5:
                a = 0.5
            cols.append((phx + c - pf, a))
        poly = QPolygonF()
        for x, a in cols:
            poly.append(QPointF(x, mid - a))
        for x, a in reversed(cols):
            poly.append(QPointF(x, mid + a))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(poly)

    # ------------------------------------------------------------- painting
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        painter.fillRect(0, 0, w, h, self.BG_COLOR)

        if self._mode == 'vu':
            self._paint_vu(painter, w, h)
        elif self._mode == 'spectrum':
            self._paint_spectrum(painter, w, h)
        elif self._mode == 'oscilloscope':
            self._paint_scope(painter, w, h)
        else:
            self._paint_waveform(painter, w, h)

        # mode label + bottom border (fillRect keeps them crisp —
        # antialiased 1px lines at integer coords would blur to 50% gray)
        painter.setPen(self.LABEL_COLOR)
        painter.setFont(self._label_font)
        painter.drawText(QRectF(w - 46, 1, 44, 11),
                         Qt.AlignmentFlag.AlignRight
                         | Qt.AlignmentFlag.AlignVCenter,
                         self.MODE_LABELS[self._mode])
        painter.fillRect(0, h - 1, w, 1, self.BORDER_COLOR)
        painter.end()

    def _paint_waveform(self, painter, w, h):
        if not self._peaks:
            # placeholder: flat line across the middle
            painter.fillRect(0, h // 2, w, 1, self.PLACEHOLDER_COLOR)
            return

        total = len(self._peaks)
        phx = int(w * self.PLAYHEAD_RATIO)
        mid = h / 2.0
        scale = h / 2.0 - 2
        pf = self._current_position() / 1000.0 * self.COLS_PER_SECOND
        if pf > total:
            pf = float(total)

        c0 = max(0, int(pf - phx))
        c1 = min(total - 1, int(pf - phx + w) + 1)
        ph_col = int(pf)  # last column at/behind the playhead

        self._band(painter, c0, min(c1, ph_col), pf, phx, mid, scale,
                   self.ACCENT_COLOR)
        self._band(painter, ph_col + 1, c1, pf, phx, mid, scale,
                   self.DIM_COLOR)

        # playhead marker
        painter.fillRect(phx, 0, 1, h, self.PLAYHEAD_COLOR)

    def _paint_vu(self, painter, w, h):
        """Two VU bars (L left, R right). Mono audio feeds both the same."""
        bar_w = 44
        gap = 12
        top, bottom = 4, h - 4
        height = bottom - top
        cx = w / 2.0
        painter.setFont(self._label_font)
        for x, vu, letter in ((cx - gap / 2 - bar_w, self._vu_l, 'L'),
                              (cx + gap / 2, self._vu_r, 'R')):
            x = int(round(x))
            painter.fillRect(x, top, int(bar_w), height,
                             QColor('#2a2a2a'))
            fill_h = int(round(min(1.0, vu) * (height - 2)))
            if fill_h > 0:
                painter.fillRect(x + 1, bottom - fill_h, int(bar_w) - 2,
                                 fill_h, self.ACCENT_COLOR)
            painter.setPen(self.LABEL_COLOR)
            painter.drawText(QRectF(x, h - 14, bar_w, 12),
                             Qt.AlignmentFlag.AlignCenter, letter)

    def _paint_spectrum(self, painter, w, h):
        """Continuous frequency-bin graph: one 1px-wide column per pixel,
        no gaps, in the same gold as the other visualizers.

        Rendered as a single QImage blit (numpy-built, one drawImage call)
        instead of 260 fillRects. The image is transparent everywhere
        except the bars, so it composites over the background fill.
        """
        if self._spec is None:
            painter.fillRect(0, h // 2, w, 1, self.PLACEHOLDER_COLOR)
            return
        spec = self._spec
        # resample the 96 data bands across the full width (smooth
        # envelope -> one continuous graph)
        values = np.interp(np.linspace(0, spec.size - 1, w),
                           np.arange(spec.size), spec)
        heights = np.clip(
            np.round(values * (h - 3)).astype(np.int32), 0, h - 3)
        rows = np.arange(h)[:, None]
        inside = ((rows >= (h - 2 - heights)[None, :])
                  & (rows <= h - 3))
        ar, ag, ab = self.ACCENT_COLOR.getRgb()[:3]
        # ARGB32 in memory is B,G,R,A; gold bars over transparent rest
        bgra = np.zeros((h, w, 4), dtype=np.uint8)
        bgra[inside] = (ab, ag, ar, 255)
        img = QImage(bgra.tobytes(), w, h, w * 4,
                     QImage.Format.Format_ARGB32)
        painter.drawImage(0, 0, img)

    def _paint_scope(self, painter, w, h):
        """The current audio waveform, like a real oscilloscope."""
        mid = h / 2.0
        painter.fillRect(0, int(mid), w, 1, QColor('#333333'))  # axis
        mono = self.scope_array()
        if mono is None or len(mono) < 2:
            return
        n = len(mono)
        step = (w - 1) / (n - 1)
        amp = h / 2.0 - 3
        poly = QPolygonF()
        for i in range(n):
            poly.append(QPointF(i * step, mid - float(mono[i]) * amp))
        painter.setPen(QPen(self.ACCENT_COLOR, 1.4))
        painter.drawPolyline(poly)


class CoverArtOverlay(QWidget):
    """Borderless, transparent, topmost FULL-SCREEN overlay on one screen:
    the current track's full-resolution cover, centered, with a self-drawn
    drop shadow, fading in and spinning into view (~500 ms, ease-out).

    Everything (shadow + art) is painted by the overlay itself — child
    widgets are not composited into a translucent top-level window
    reliably, and QWidget has no rotation API, so the spin is a painter
    transform about the art's center.

    Dismissed by any key press, a click outside the art (including the
    shadow margin), or losing focus (clicking elsewhere on the desktop).
    """
    IN_MS = 500
    OUT_MS = 160
    SPIN_DEG = -90.0
    _SHADOW_MARGIN = 64  # how far the (pre-rendered) shadow extends
    _SHADOW_STEPS = 24   # layered rounded rects approximating a blur

    def __init__(self, pixmap, screen):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.Tool
                             | Qt.WindowType.FramelessWindowHint
                             | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        geo = screen.geometry()
        self.setGeometry(geo)
        # show at full resolution, capped so it fits the screen — and
        # never upscale, so the art stays pixel-perfect
        avail = screen.availableGeometry()
        size = pixmap.size().scaled(
            int(avail.width() * 0.85), int(avail.height() * 0.85),
            Qt.AspectRatioMode.KeepAspectRatio)
        if size.width() < pixmap.width() or size.height() < pixmap.height():
            self._shown = pixmap.scaled(
                size, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
        else:
            self._shown = pixmap
        # same rounded-corner crop as the panel's cover box, scaled
        self._radius = max(1, round(TrackInfoPanel.ART_RADIUS
                                    * self._shown.width()
                                    / TrackInfoPanel.ART_SIZE))
        self._art_rect = QRectF(
            (geo.width() - self._shown.width()) / 2.0,
            (geo.height() - self._shown.height()) / 2.0,
            self._shown.width(), self._shown.height())
        self._shadow_pm = self._make_shadow(
            self._shown.width(), self._shown.height(), self._radius)
        self._shadow_rect = QRectF(
            self._art_rect.x() - self._SHADOW_MARGIN,
            self._art_rect.y() - self._SHADOW_MARGIN,
            self._shown.width() + 2 * self._SHADOW_MARGIN,
            self._shown.height() + 2 * self._SHADOW_MARGIN)
        self._closing = False
        self._spin_deg = self.SPIN_DEG
        self._alpha = 0.0
        self._progress = 0.0
        # fade + spin in, driven by one eased progress property
        anim = QPropertyAnimation(self, b'progress', self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(self.IN_MS)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._in_anim = anim
        self._out_anim = None
        anim.start()

    # -------------------------------------------------------- pre-render
    def _make_shadow(self, w, h, radius):
        """Soft drop shadow as concentric rounded rects (outer, faintest
        first) — pre-rendered once, drawn every frame under the same
        transform as the art so it rotates with it."""
        m = self._SHADOW_MARGIN
        steps = self._SHADOW_STEPS
        pm = QPixmap(w + 2 * m, h + 2 * m)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(steps, 0, -1):
            grow = int(i * m / steps)
            t = 1.0 - i / steps
            alpha = int(110 * (t * t) ** 0.75)
            if alpha <= 0:
                continue
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(
                QRect(m - grow, m - grow, w + 2 * grow, h + 2 * grow),
                radius + grow, radius + grow)
        painter.end()
        return pm

    # -------------------------------------------------------------- props
    def get_progress(self):
        return self._progress

    def set_progress(self, value):
        self._progress = float(value)
        self._spin_deg = self.SPIN_DEG * (1.0 - self._progress)
        self._alpha = self._progress
        self.update()

    progress = pyqtProperty(float, get_progress, set_progress)

    def get_alpha(self):
        return self._alpha

    def set_alpha(self, value):
        self._alpha = float(value)
        self.update()

    alpha = pyqtProperty(float, get_alpha, set_alpha)

    # ------------------------------------------------------------ dismiss
    def dismiss(self):
        """Fade out quickly and close (any key / outside click / focus loss)."""
        if self._closing or not self.isVisible():
            return
        self._closing = True
        self._in_anim.stop()
        self._spin_deg = 0.0
        fade = QPropertyAnimation(self, b'alpha', self)
        fade.setStartValue(float(self._alpha))
        fade.setEndValue(0.0)
        fade.setDuration(self.OUT_MS)
        fade.setEasingCurve(QEasingCurve.Type.InCubic)
        fade.finished.connect(self.close)
        self._out_anim = fade
        fade.start()

    # -------------------------------------------------------------- events
    def showEvent(self, event):
        super().showEvent(event)
        self.activateWindow()
        self.setFocus()

    def keyPressEvent(self, event):
        self.dismiss()

    def mousePressEvent(self, event):
        # a click anywhere outside the art dismisses; a click on the art
        # itself keeps it up
        if not self._art_rect.contains(event.position()):
            self.dismiss()
        super().mousePressEvent(event)

    def focusOutEvent(self, event):
        self.dismiss()
        super().focusOutEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        center = self._art_rect.center()
        painter.translate(center)
        painter.rotate(self._spin_deg)
        painter.translate(-center)
        painter.setOpacity(self._alpha)
        painter.drawPixmap(
            int(self._shadow_rect.x()), int(self._shadow_rect.y()),
            int(self._shadow_rect.width()), int(self._shadow_rect.height()),
            self._shadow_pm)
        path = QPainterPath()
        path.addRoundedRect(self._art_rect, self._radius, self._radius)
        painter.setClipPath(path)
        painter.drawPixmap(
            int(self._art_rect.x()), int(self._art_rect.y()),
            int(self._art_rect.width()), int(self._art_rect.height()),
            self._shown)


class TrackInfoPanel(QWidget):
    """Right-side panel that displays the current track's metadata.

    Part of the main window, docked to the right of the main content behind
    a thin vertical separator (the menu bar row only covers the left column,
    so it stops at that separator). A waveform visualizer spans the full
    width at the top; below it, cover art, title, artist, album, year and
    genre (when present in the file metadata) are vertically centered.
    """
    WIDTH = 260
    ART_SIZE = 220
    ART_RADIUS = 10

    # emitted when the cover art (a real one, not the placeholder) is
    # clicked — the main window opens the full-resolution cover overlay
    cover_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_path: Optional[Path] = None
        self._full_cover: Optional[QPixmap] = None

        self.setFixedWidth(self.WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Waveform visualizer spanning the entire width of the panel
        self.waveform = WaveformVisualizer(self)
        outer.addWidget(self.waveform)

        inner = QWidget(self)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        outer.addWidget(inner)

        layout.addStretch(1)

        # Cover art (or a placeholder glyph when the track has none)
        self.cover_label = QLabel(self)
        self.cover_label.setFixedSize(self.ART_SIZE, self.ART_SIZE)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet(
            "background-color: #2d2d2d; border-radius: "
            f"{self.ART_RADIUS}px;")
        self.cover_label.installEventFilter(self)
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
        self.waveform.set_track(filepath)
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
                # keep the original (full resolution) for the overlay
                self._full_cover = pixmap
                scaled = pixmap.scaled(
                    self.ART_SIZE, self.ART_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self.cover_label.setPixmap(self._round_pixmap(scaled))
                self.cover_glyph.setVisible(False)
                self._update_cover_clickability()
                return
        self._full_cover = None
        self.cover_label.setPixmap(QPixmap())
        self.cover_glyph.setVisible(True)
        self._update_cover_clickability()

    def clear(self):
        """Reset the panel to the 'no track' state."""
        self.current_path = None
        self.waveform.clear()
        self.title_label.setText('No track loaded')
        self.title_label.setVisible(True)
        for lbl in (self.artist_label, self.album_label, self.extra_label):
            self._set_field(lbl, None)
        self._full_cover = None
        self.cover_label.setPixmap(QPixmap())
        self.cover_glyph.setVisible(True)
        self._update_cover_clickability()

    def full_cover_pixmap(self) -> Optional[QPixmap]:
        """The current track's full-resolution cover, or None."""
        return self._full_cover

    def _update_cover_clickability(self):
        has_art = self._full_cover is not None
        self.cover_label.setCursor(
            Qt.CursorShape.PointingHandCursor if has_art
            else Qt.CursorShape.ArrowCursor)
        self.cover_label.setToolTip(
            'Click to view the cover in full size' if has_art else '')

    def eventFilter(self, obj, event):
        if (obj is self.cover_label
                and event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and self._full_cover is not None):
            self.cover_clicked.emit()
            return True
        return super().eventFilter(obj, event)

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
        # The view the UI and playback actually use: all songs, or (when
        # saved_only is on) just the saved ones. current_index is an index
        # into visible_songs, never into playlist.
        self.visible_songs: List[Path] = []
        self.saved_only: bool = False
        self.current_index: int = -1
        # Per-song info caches: reading each file's tags (name/length/saved
        # flag) on every playlist rebuild is what made large lists lag.
        # These are populated on first read and invalidated whenever a file
        # is added/removed/deleted or its saved state is toggled.
        self._name_cache: dict = {}
        self._length_cache: dict = {}
        self._saved_cache: dict = {}
        # Chunked playlist rebuild state (see _rebuild_playlist_widget)
        self._rebuild_token: int = 0
        self._rebuild_in_progress: bool = False
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
        self.player.playbackStateChanged.connect(self.on_playback_state_changed)
        
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
        
        # Saved-only filter button (trophy), right of the delete button
        self.saved_only_button = GlyphCenteredButton("🏆")
        self.saved_only_button.setStyleSheet(BUTTON_FONT_SIZE)
        self.saved_only_button.setFixedSize(button_size, button_size)
        self.saved_only_button.clicked.connect(self.toggle_saved_only)
        self.saved_only_button.setToolTip("Show only saved songs (Ctrl+F)")
        
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
        controls_layout.addWidget(self.saved_only_button)
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
        self._cover_overlay = None
        self.info_panel.cover_clicked.connect(self.show_cover_full)
        
        root_layout.addWidget(left_widget, 1)
        root_layout.addWidget(panel_separator)
        root_layout.addWidget(self.info_panel)

    def show_cover_full(self):
        """Open the full-resolution cover in a borderless, transparent,
        topmost overlay centered on the SAME screen as the main window
        (multi-monitor aware). Any key, a click outside the art, or
        losing focus dismisses it."""
        pixmap = self.info_panel.full_cover_pixmap()
        if pixmap is None or pixmap.isNull():
            return  # placeholder (no embedded art) — nothing to open
        if self._cover_overlay is not None:
            if self._cover_overlay.isVisible():
                self._cover_overlay.dismiss()  # toggle closed
                return
            self._cover_overlay = None  # stale (already deleted)
        screen = QGuiApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        overlay = CoverArtOverlay(pixmap, screen)
        overlay.destroyed.connect(
            lambda: setattr(self, '_cover_overlay', None))
        self._cover_overlay = overlay
        overlay.show()
        overlay.raise_()
        overlay.activateWindow()
    
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
        if 0 <= self.current_index < len(self.visible_songs):
            self.info_panel.set_track(self.visible_songs[self.current_index])
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
        self.addAction(self.create_shortcut("Ctrl+F", self.toggle_saved_only))
    
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
    
    def _rebuild_visible_songs(self):
        """(Re)compute the playlist view the UI and playback use."""
        if self.saved_only:
            self.visible_songs = [
                p for p in self.playlist if self.is_song_saved(p)]
        else:
            # alias: incremental playlist edits stay in sync automatically
            self.visible_songs = self.playlist

    # Small lists build in one go; bigger ones stream in across event-loop
    # passes (a handful of rows per pass) so the UI never freezes and the
    # rows appear in realtime as they are enumerated.
    _REBUILD_SYNC_MAX = 150
    _REBUILD_CHUNK = 32

    def _rebuild_playlist_widget(self):
        """Repopulate the playlist table from the visible view."""
        self._rebuild_token += 1  # cancels any chunked build in flight
        token = self._rebuild_token
        if len(self.visible_songs) <= self._REBUILD_SYNC_MAX:
            self.playlist_widget.setRowCount(0)
            self._rebuild_in_progress = False
            for f in self.visible_songs:
                self.add_to_playlist_widget(f)
            self._after_playlist_rebuild()
            return
        self.playlist_widget.setRowCount(0)
        self._rebuild_in_progress = True
        QTimer.singleShot(0, lambda: self._rebuild_chunk(token))

    def _rebuild_chunk(self, token: int):
        """Add the next batch of rows; reschedule until the list is full.
        A newer rebuild bumps _rebuild_token, which stops this loop."""
        if token != self._rebuild_token:
            return
        total = len(self.visible_songs)
        rows = self.playlist_widget.rowCount()
        end = min(rows + self._REBUILD_CHUNK, total)
        for i in range(rows, end):
            self.add_to_playlist_widget(self.visible_songs[i])
        if self.playlist_widget.rowCount() < total:
            QTimer.singleShot(0, lambda: self._rebuild_chunk(token))
        else:
            self._rebuild_in_progress = False
            self._after_playlist_rebuild()

    def _after_playlist_rebuild(self):
        """Highlight the current row once the (possibly chunked) rebuild
        has finished — the rows may not exist yet mid-stream."""
        if not self._rebuild_in_progress and self.current_index >= 0:
            self.highlight_current_song()

    def _next_seq_index(self, index: int, count: int) -> int:
        """Next index in the playback sequence (same rules as
        get_next_index, but for an explicit index/count)."""
        if count == 0:
            return -1
        if self.play_order == PlayOrder.OLDEST_TO_NEWEST:
            return (index + 1) % count
        next_idx = index - 1
        if next_idx < 0:
            next_idx = count - 1
        return next_idx

    def _clear_current_song(self):
        """Drop the current selection (no song loaded state)"""
        self.current_index = -1
        self.song_label.setText("No song loaded")
        self.play_pause_button.setText("▶")
        self._update_track_info()
        self.highlight_current_song()

    def _apply_saved_only_filter(self):
        """Rebuild the visible playlist after the saved-only filter (or a
        song's saved state) changed.

        - If the current song survives, keep playing/paused exactly where
          it was and just remap its (moved) index.
        - If it drops out, the next song is the next saved song in the
          playback sequence; playback continues if it was running.
        - If nothing was selected, pick the normal starting song.
        """
        was_playing = (self.player.playbackState()
                       == QMediaPlayer.PlaybackState.PlayingState)
        old_visible = self.visible_songs
        old_index = self.current_index
        current_file = (old_visible[old_index]
                        if 0 <= old_index < len(old_visible) else None)

        self._rebuild_visible_songs()
        survived = (current_file is not None
                    and current_file in self.visible_songs)

        # next song in the sequence that remains visible (for the case the
        # current song drops out of the filtered view)
        next_file = None
        if current_file is not None and not survived:
            i, n = old_index, len(old_visible)
            for _ in range(n):
                i = self._next_seq_index(i, n)
                candidate = old_visible[i]
                if candidate in self.visible_songs:
                    next_file = candidate
                    break

        self._rebuild_playlist_widget()

        if survived:
            self.current_index = self.visible_songs.index(current_file)
            self.highlight_current_song()
        elif current_file is not None:
            self.player.stop()
            if next_file is not None:
                self.current_index = self.visible_songs.index(next_file)
                self.load_current_song()
                if was_playing:
                    self.player.play()
                    self.play_pause_button.setText("⏸")
                else:
                    self.play_pause_button.setText("▶")
            else:
                self._clear_current_song()
        elif self.visible_songs:
            self.current_index = self.get_starting_index()
            self.load_current_song()
            self.highlight_current_song()

        self.reset_delete_state()
        self.update_save_button()

    def toggle_saved_only(self):
        """Toggle the 'saved songs only' playlist filter (trophy button /
        Ctrl+F)."""
        self.saved_only = not self.saved_only
        self.update_saved_only_button()
        self.save_config()
        self._apply_saved_only_filter()

    def update_saved_only_button(self):
        """Highlight the trophy button while the filter is on"""
        if self.saved_only:
            self.saved_only_button.setStyleSheet(f"""
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
            self.saved_only_button.setStyleSheet(BUTTON_FONT_SIZE)

    def get_next_index(self):
        """Get the next song index based on play order"""
        if not self.visible_songs:
            return -1
        
        if self.play_order == PlayOrder.OLDEST_TO_NEWEST:
            # Moving forward through the list (bottom to top in display)
            return (self.current_index + 1) % len(self.visible_songs)
        else:  # NEWEST_TO_OLDEST
            # Moving backward through the list (top to bottom in display)
            next_idx = self.current_index - 1
            if next_idx < 0:
                next_idx = len(self.visible_songs) - 1
            return next_idx
    
    def get_previous_index(self):
        """Get the previous song index based on play order"""
        if not self.visible_songs:
            return -1
        
        if self.play_order == PlayOrder.OLDEST_TO_NEWEST:
            # Moving backward through the list (top to bottom in display)
            prev_idx = self.current_index - 1
            if prev_idx < 0:
                prev_idx = len(self.visible_songs) - 1
            return prev_idx
        else:  # NEWEST_TO_OLDEST
            # Moving forward through the list (bottom to top in display)
            return (self.current_index + 1) % len(self.visible_songs)
    
    def get_starting_index(self):
        """Get the index to start playing from based on play order"""
        if not self.visible_songs:
            return -1
        
        if self.play_order == PlayOrder.OLDEST_TO_NEWEST:
            # Start at the end (oldest song, which is at bottom)
            return len(self.visible_songs) - 1
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
        self._name_cache.clear()
        self._length_cache.clear()
        self._saved_cache.clear()
        
        # Add files in order (most recent first, so they appear at top)
        for file in files:
            self.playlist.append(file)
        
        # Refresh the view and the table
        self._rebuild_visible_songs()
        self._rebuild_playlist_widget()
        
        # Start playing from the appropriate position based on play order
        if self.visible_songs and self.current_index == -1:
            self.current_index = self.get_starting_index()
            self.load_current_song()
            self.highlight_current_song()
    
    def add_file_to_playlist(self, filepath: str):
        """Add a new file to the playlist"""
        path = Path(filepath)
        if path not in self.playlist:
            # The file on disk may be new (or different from one that was
            # here before): forget any cached info for this path
            self._invalidate_song_caches(path)
            # Insert at the beginning (top) of the canonical playlist
            self.playlist.insert(0, path)
            self._rebuild_visible_songs()
            
            # Only unsaved songs are hidden by the saved-only filter
            if path in self.visible_songs:
                # Adjust current index if necessary
                if self.current_index >= 0:
                    self.current_index += 1
                if self._rebuild_in_progress:
                    # A chunked rebuild is streaming in: restart it so the
                    # new row lands in the right place (in order)
                    self._rebuild_playlist_widget()
                else:
                    self.add_to_playlist_widget_at_top(path)
                    # Update the highlighted row to match the new index
                    if self.current_index >= 0:
                        self.highlight_current_song()
            
            # If nothing is playing, start playing from the appropriate position
            if self.current_index == -1 and self.visible_songs:
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
        
        was_visible = path in self.visible_songs
        index = self.visible_songs.index(path) if was_visible else -1
        was_current = (index == self.current_index)
        
        # Keep the remote consistent: if it was a saved song, remove it
        # from the server (no-op there if it doesn't exist). The file is
        # already gone, so the saved state comes from the row's cached flag
        # (kept in sync by toggle_save_song) — not a fresh tag read.
        item = (self.playlist_widget.item(index, 0)
                if was_visible else None)
        was_saved = (bool(item.data(Qt.ItemDataRole.UserRole + 1))
                     if item is not None else self.is_song_saved(path))
        if was_saved:
            self.sync_remove(self.get_remote_name(path))
        
        self._invalidate_song_caches(path)
        self.playlist.remove(path)
        self._rebuild_visible_songs()
        if was_visible:
            if self._rebuild_in_progress:
                # mid-stream rebuild: let a fresh rebuild place everything
                self._rebuild_playlist_widget()
            else:
                self.playlist_widget.removeRow(index)
        
        if was_current:
            self.player.stop()
            if self.visible_songs:
                # The next song slid into this index
                if self.current_index >= len(self.visible_songs):
                    self.current_index = 0
                self.load_current_song()
                self.player.play()
                self.play_pause_button.setText("⏸")
            else:
                self._clear_current_song()
        else:
            if was_visible and index < self.current_index:
                self.current_index -= 1
            if self.current_index >= 0:
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
        """Get formatted length (M:SS) of an audio file (cached)."""
        return self._cached_song(self._length_cache, filepath,
                                 self._read_song_length)

    def _read_song_length(self, filepath: Path) -> str:
        length = "?"
        try:
            audio = mutagen.File(filepath)
            if audio and audio.info:
                length = self.format_time(int(audio.info.length * 1000))
        except:
            pass
        return length

    def _cached_song(self, cache: dict, filepath: Path, reader) -> str:
        """Memoize reader(filepath) on the file's mtime.

        A stat() is cheap, so unchanged files cost nothing (no tag parse)
        on every access, while an external rewrite of the file bumps its
        mtime and is picked up automatically. Cache values are (mtime,
        value) tuples keyed by path."""
        hit = cache.get(filepath)
        try:
            mtime = filepath.stat().st_mtime
        except OSError:
            # File is gone: return the last known value (the file may have
            # been deleted out from under us) rather than re-reading.
            if hit is not None:
                return hit[1]
            return reader(filepath)
        if hit is not None and hit[0] == mtime:
            return hit[1]
        value = reader(filepath)
        cache[filepath] = (mtime, value)
        return value

    def _invalidate_song_caches(self, filepath: Path):
        """Forget cached info for one file (it was added/removed/deleted)."""
        self._name_cache.pop(filepath, None)
        self._length_cache.pop(filepath, None)
        self._saved_cache.pop(filepath, None)
    
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
                index = self.visible_songs.index(filepath)

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
        if 0 <= self.current_index < len(self.visible_songs):
            filepath = self.visible_songs[self.current_index]
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
        """Extract song name from metadata or use filename (cached)."""
        return self._cached_song(self._name_cache, filepath,
                                 self._read_song_name)

    def _read_song_name(self, filepath: Path) -> str:
        """Read the song name straight from the file (no cache)."""
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
        """Check if a song is marked as saved (embedded metadata tag).
        Memoized on mtime (see _cached_song): cheap for unchanged files,
        and an external re-tag bumps mtime so it is picked up."""
        return self._cached_song(self._saved_cache, filepath,
                                 read_saved_tag)
    
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
        if self.current_index < 0 or self.current_index >= len(self.visible_songs):
            return
        
        current_file = self.visible_songs[self.current_index]
        
        if not current_file.exists():
            return
        
        now_saved = not self.is_song_saved(current_file)
        if not set_saved_tag(current_file, now_saved):
            self._show_status("Could not update saved status", 3000)
            return
        # The tag write bumped mtime; drop the stale cache entry so the
        # next read re-parses and re-caches under the new mtime.
        self._saved_cache.pop(current_file, None)
        
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
        
        if self.saved_only and not now_saved:
            # This song just dropped out of the saved-only playlist:
            # the next song to play is the next saved one in the sequence
            self._apply_saved_only_filter()
    
    def update_save_button(self):
        """Update save button appearance based on current song's save status"""
        if self.current_index >= 0 and self.current_index < len(self.visible_songs):
            current_file = self.visible_songs[self.current_index]
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
        if (self.current_index < 0
                or self.current_index >= len(self.visible_songs)):
            return
        
        current_file = self.visible_songs[self.current_index]
        
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
            self._invalidate_song_caches(current_file)
            
            # Remote sync: remove from remote server if it exists
            self.sync_remove(self.get_remote_name(current_file))
            
            # Remove from the canonical playlist and the visible view.
            # When the filter is off, visible_songs aliases playlist, so
            # remove only ONCE (a second removal would eat the next song).
            if self.visible_songs is self.playlist:
                del self.visible_songs[self.current_index]
            else:
                self.playlist.remove(current_file)
                del self.visible_songs[self.current_index]
            if self._rebuild_in_progress:
                self._rebuild_playlist_widget()
            else:
                self.playlist_widget.removeRow(self.current_index)
            
            # Move to next song or stop if no more songs. After removal,
            # current_index points at whatever slid into its place; wrap
            # if we deleted the last row.
            if self.visible_songs:
                if self.current_index >= len(self.visible_songs):
                    self.current_index = 0
                
                # Load and play next song
                self.load_current_song()
                self.player.play()
                self.play_pause_button.setText("⏸")
            else:
                # No more songs
                self._clear_current_song()
            
        except Exception as e:
            print(f"Error deleting file: {e}")
    
    def toggle_play_pause(self):
        """Toggle between play and pause"""
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_pause_button.setText("▶")
        else:
            if self.current_index == -1 and self.visible_songs:
                self.current_index = self.get_starting_index()
                self.load_current_song()
            self.player.play()
            self.play_pause_button.setText("⏸")
    
    def next_song(self):
        """Skip to next song"""
        if not self.visible_songs:
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
        if not self.visible_songs:
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
        if self.info_panel is not None:
            self.info_panel.waveform.seek_audio(position)
    
    def update_position(self, position):
        """Update position display"""
        self.seek_slider.setValue(position)
        self.time_label.setText(self.format_time(position))
        if self.info_panel is not None:
            self.info_panel.waveform.note_position(position)
    
    def on_playback_state_changed(self, state):
        """Run/pause the visualizer animation and audio tap with the
        playback state"""
        if self.info_panel is not None:
            playing = state == QMediaPlayer.PlaybackState.PlayingState
            self.info_panel.waveform.set_playing(playing)
    
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
                    if self.current_index < len(self.visible_songs) - 1:
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
            
            # Apply the scalar settings FIRST: set_target_directory builds
            # the playlist immediately, and it must already use the saved
            # initial_songs_count / play_order.
            if 'loop_mode' in config:
                self.loop_mode = LoopMode(config['loop_mode'])
                self.update_loop_button()
            
            if 'play_order' in config:
                self.play_order = PlayOrder(config['play_order'])
            
            if 'saved_only' in config:
                self.saved_only = bool(config['saved_only'])
                self.update_saved_only_button()
            
            if 'visualizer' in config:
                if self.info_panel is not None:
                    self.info_panel.waveform.set_mode(config['visualizer'])
            
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
            
            if 'target_directory' in config:
                directory = Path(config['target_directory'])
                if directory.exists():
                    self.set_target_directory(directory)
                
        except Exception as e:
            print(f"Error loading config: {e}")
    
    def save_config(self):
        """Save configuration to file"""
        self.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        
        config = {
            'loop_mode': self.loop_mode.value,
            'play_order': self.play_order.value,
            'saved_only': self.saved_only,
            'visualizer': (self.info_panel.waveform.mode()
                           if self.info_panel is not None else 'waveform'),
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
        
        # Stop the live audio tap (stops the in-process PyAV decoder)
        if self.info_panel is not None:
            self.info_panel.waveform.stop_audio_tap()
        
        # Dismiss the full-resolution cover overlay, if open
        if self._cover_overlay is not None:
            self._cover_overlay.close()
        
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