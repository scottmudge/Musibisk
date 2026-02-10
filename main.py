#!/usr/bin/env python3
"""
Musibisk - A sleek, minimal music player with directory monitoring
"""

import sys
import json
import os
from pathlib import Path
from typing import List, Optional
from enum import Enum
import base64

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSlider, QFileDialog, QMenuBar, QMenu,
    QListWidget, QListWidgetItem, QDialog, QFormLayout, QSpinBox,
    QDialogButtonBox
)
from PyQt6.QtCore import (
    Qt, QTimer, QUrl, QThread, pyqtSignal, QObject, QByteArray
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtGui import QAction, QKeySequence, QIcon, QPixmap
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import mutagen
from icon import ICON_PNG_BASE64

from icon import ICON_PNG_BASE64

class LoopMode(Enum):
    NO_LOOP = 0
    LOOP_PLAYLIST = 1
    LOOP_SINGLE = 2
    
    
def icon_from_base64_png(b64: str) -> QIcon:
    raw = base64.b64decode(b64)
    ba = QByteArray(raw)

    pixmap = QPixmap()
    pixmap.loadFromData(ba, "PNG")

    return QIcon(pixmap)


class FileWatcherHandler(FileSystemEventHandler):
    """Handles file system events for audio files"""
    
    AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.opus', '.aac', '.wma'}
    
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
    
    def on_created(self, event):
        if not event.is_directory:
            ext = Path(event.src_path).suffix.lower()
            if ext in self.AUDIO_EXTENSIONS:
                self.callback(event.src_path)


class FileWatcherThread(QThread):
    """Thread for watching directory changes"""
    file_added = pyqtSignal(str)
    
    def __init__(self, directory: str):
        super().__init__()
        self.directory = directory
        self.observer = None
        self._stop_requested = False
        
    def run(self):
        handler = FileWatcherHandler(self.file_added.emit)
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


class SettingsDialog(QDialog):
    """Settings dialog for configuring Musibisk"""
    
    def __init__(self, parent=None, initial_songs=50):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setFixedSize(300, 120)
        
        layout = QFormLayout(self)
        
        self.songs_spinbox = QSpinBox()
        self.songs_spinbox.setRange(1, 1000)
        self.songs_spinbox.setValue(initial_songs)
        self.songs_spinbox.setSuffix(" songs")
        
        layout.addRow("Initial playlist size:", self.songs_spinbox)
        
        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        
        self.apply_style()
    
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
            QSpinBox {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                border-radius: 4px;
                padding: 4px;
            }
            QSpinBox::up-button, QSpinBox::down-button {
                background-color: #3d3d3d;
                border: 1px solid #4d4d4d;
            }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {
                background-color: #4d4d4d;
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


class Musibisk(QMainWindow):
    """Main application window"""
    
    CONFIG_DIR = Path.home() / '.config' / 'musibisk'
    CONFIG_FILE = CONFIG_DIR / 'config.json'
    AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.opus', '.aac', '.wma'}
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Musibisk")
        self.setFixedSize(420, 400)
        
        # State
        self.playlist: List[Path] = []
        self.current_index: int = -1
        self.loop_mode = LoopMode.NO_LOOP
        self.target_directory: Optional[Path] = None
        self.watcher_thread: Optional[FileWatcherThread] = None
        self.initial_songs_count: int = 50
        
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
        
    def init_ui(self):
        """Initialize the user interface"""
        # Menu bar
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        
        select_folder_action = QAction("Select Target Folder", self)
        select_folder_action.triggered.connect(self.select_folder)
        file_menu.addAction(select_folder_action)
        
        file_menu.addSeparator()
        
        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self.show_settings)
        file_menu.addAction(settings_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(15, 5, 15, 15)
        layout.setSpacing(8)
        
        # Directory label at top
        self.dir_label = QLabel("No directory selected")
        self.dir_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dir_label.setStyleSheet("font-size: 10px; color: #888; padding: 2px;")
        self.dir_label.setMaximumHeight(16)
        layout.addWidget(self.dir_label)
        
        # Playlist view
        self.playlist_widget = QListWidget()
        self.playlist_widget.setMaximumHeight(180)
        self.playlist_widget.itemDoubleClicked.connect(self.on_playlist_item_clicked)
        layout.addWidget(self.playlist_widget)
        
        # Song info
        self.song_label = QLabel("No song loaded")
        self.song_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.song_label.setStyleSheet("font-size: 14px; font-weight: bold; padding: 4px 0;")
        self.song_label.setMaximumHeight(30)
        layout.addWidget(self.song_label)
        
        # Time info
        time_layout = QHBoxLayout()
        time_layout.setContentsMargins(0, 0, 0, 0)
        self.time_label = QLabel("0:00")
        self.time_label.setStyleSheet("font-size: 11px; color: #aaa;")
        self.duration_label = QLabel("0:00")
        self.duration_label.setStyleSheet("font-size: 11px; color: #aaa;")
        time_layout.addWidget(self.time_label)
        time_layout.addStretch()
        time_layout.addWidget(self.duration_label)
        layout.addLayout(time_layout)
        
        # Seek bar
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
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
        
        self.prev_button = QPushButton("⏮")
        self.prev_button.setFixedSize(button_size, button_size)
        self.prev_button.clicked.connect(self.previous_song)
        
        self.play_pause_button = QPushButton("▶")
        self.play_pause_button.setFixedSize(button_size, button_size)
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        
        self.next_button = QPushButton("⏭")
        self.next_button.setFixedSize(button_size, button_size)
        self.next_button.clicked.connect(self.next_song)
        
        self.loop_button = QPushButton("🔁")
        self.loop_button.setFixedSize(button_size, button_size)
        self.loop_button.clicked.connect(self.toggle_loop_mode)
        self.update_loop_button()
        
        controls_layout.addStretch()
        controls_layout.addWidget(self.prev_button)
        controls_layout.addWidget(self.play_pause_button)
        controls_layout.addWidget(self.next_button)
        controls_layout.addWidget(self.loop_button)
        controls_layout.addStretch()
        
        layout.addLayout(controls_layout)
    
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
            QListWidget {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                border-radius: 6px;
                padding: 4px;
                font-size: 11px;
            }
            QListWidget::item {
                padding: 4px;
                border-radius: 3px;
            }
            QListWidget::item:selected {
                background-color: #4d9eff;
                color: #ffffff;
            }
            QListWidget::item:hover {
                background-color: #3d3d3d;
            }
            QPushButton {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                border-radius: 10px;
                font-size: 18px;
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
        """)
    
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
        dialog = SettingsDialog(self, self.initial_songs_count)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.initial_songs_count = dialog.get_songs_count()
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
        self.watcher_thread.start()
        
        # Save config
        self.save_config()
    
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
        self.playlist_widget.clear()
        
        for file in files:
            self.playlist.append(file)
            self.add_to_playlist_widget(file)
        
        # Start playing first song if playlist not empty
        if self.playlist and self.current_index == -1:
            self.current_index = 0
            self.load_current_song()
            self.highlight_current_song()
    
    def add_file_to_playlist(self, filepath: str):
        """Add a new file to the playlist"""
        path = Path(filepath)
        if path not in self.playlist:
            self.playlist.append(path)
            self.add_to_playlist_widget(path)
            
            # If nothing is playing, start playing this
            if self.current_index == -1:
                self.current_index = len(self.playlist) - 1
                self.load_current_song()
                self.highlight_current_song()
                self.player.play()
    
    def add_to_playlist_widget(self, filepath: Path):
        """Add a song to the playlist widget"""
        song_name = self.get_song_name(filepath)
        item = QListWidgetItem(f"♪ {song_name}")
        item.setData(Qt.ItemDataRole.UserRole, filepath)
        self.playlist_widget.addItem(item)
    
    def on_playlist_item_clicked(self, item: QListWidgetItem):
        """Handle playlist item double-click"""
        filepath = item.data(Qt.ItemDataRole.UserRole)
        try:
            index = self.playlist.index(filepath)
            self.current_index = index
            self.load_current_song()
            self.highlight_current_song()
            self.player.play()
            self.play_pause_button.setText("⏸")
        except ValueError:
            pass
    
    def highlight_current_song(self):
        """Highlight the currently playing song in the playlist"""
        if 0 <= self.current_index < self.playlist_widget.count():
            self.playlist_widget.setCurrentRow(self.current_index)
    
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
    
    def toggle_play_pause(self):
        """Toggle between play and pause"""
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_pause_button.setText("▶")
        else:
            if self.current_index == -1 and self.playlist:
                self.current_index = 0
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
            self.current_index = (self.current_index + 1) % len(self.playlist)
            self.load_current_song()
            self.player.play()
            self.play_pause_button.setText("⏸")
    
    def previous_song(self):
        """Go to previous song"""
        if not self.playlist:
            return
        
        # If more than 3 seconds into song, restart it
        if self.player.position() > 3000:
            self.player.setPosition(0)
        else:
            self.current_index = (self.current_index - 1) % len(self.playlist)
            self.load_current_song()
            self.player.play()
            self.play_pause_button.setText("⏸")
    
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
            self.loop_button.setStyleSheet("QPushButton { color: #888; }")
        elif self.loop_mode == LoopMode.LOOP_PLAYLIST:
            self.loop_button.setText("🔁")
            self.loop_button.setStyleSheet("QPushButton { color: #4d9eff; }")
        else:  # LOOP_SINGLE
            self.loop_button.setText("🔂")
            self.loop_button.setStyleSheet("QPushButton { color: #4d9eff; }")
    
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
                # No loop - stop at end
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
            
            if 'volume' in config:
                self.audio_output.setVolume(config['volume'])
            
            if 'initial_songs_count' in config:
                self.initial_songs_count = config['initial_songs_count']
                
        except Exception as e:
            print(f"Error loading config: {e}")
    
    def save_config(self):
        """Save configuration to file"""
        self.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        
        config = {
            'loop_mode': self.loop_mode.value,
            'volume': self.audio_output.volume(),
            'initial_songs_count': self.initial_songs_count
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
        
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Musibisk")
    icon = icon_from_base64_png(ICON_PNG_BASE64)
    app.setWindowIcon(icon)
    
    window = Musibisk()
    window.show()
    
    sys.exit(app.exec())


if __name__ == '__main__':
    main()