"""Design token system and theme engine for VM-Harness GUI.

Centralized design tokens (colors, typography, spacing, shadows, border radii)
and QSS generators for consistent styling across all widgets.

Usage:
    from gui.theme import theme, T, dark_palette
    widget.setStyleSheet(theme.card_style())
"""

from __future__ import annotations

from PyQt5.QtGui import QColor, QPalette
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# Design Tokens
# ═══════════════════════════════════════════════════════════════════════════════

class T:
    """Centralized design tokens.  Import as `from gui.theme import T`."""

    # ── Color Palette ──────────────────────────────────────────────────────────

    # Base surfaces
    BG_PRIMARY   = "#0f172a"   # Main window / panel background
    BG_SECONDARY = "#1e293b"   # Cards, inputs, secondary surfaces
    BG_TERTIARY  = "#334155"   # Borders, dividers, hover states
    BG_INACTIVE  = "#172033"   # Disabled / inactive surfaces

    # Text hierarchy
    TEXT_PRIMARY   = "#e2e8f0"   # Primary text (values, labels)
    TEXT_SECONDARY = "#94a3b8"   # Secondary text (descriptions, hints)
    TEXT_MUTED     = "#64748b"   # Muted text (placeholders, disabled)
    TEXT_ACCENT    = "#60a5fa"   # Accent text (links, highlights)

    # Brand / semantic
    BRAND       = "#3b82f6"   # Primary brand color
    BRAND_HOVER = "#2563eb"   # Brand hover
    BRAND_ACTIVE= "#1d4ed8"   # Brand active
    ACCENT      = "#a78bfa"   # Accent (toggles, highlights)

    SUCCESS     = "#22c55e"   # Success / running / connected
    SUCCESS_BG  = "#22c55e20"
    WARNING     = "#f59e0b"   # Warning / pending / attention
    WARNING_BG  = "#f59e0b20"
    ERROR       = "#ef4444"   # Error / stop / disconnected
    ERROR_BG    = "#ef444420"
    INFO        = "#38bdf8"   # Info / telemetry / QMP

    # Status colors
    STATUS_RUNNING  = "#22c55e"
    STATUS_PAUSED   = "#f59e0b"
    STATUS_STOPPED  = "#ef4444"
    STATUS_OFFLINE  = "#64748b"

    # Status dot colors
    DOT_RUNNING_CONNECTED = "#22c55e"
    DOT_RUNNING           = "#eab308"
    DOT_CONNECTED         = "#3b82f6"
    DOT_OFFLINE           = "#555555"

    # Chart colors
    CHART_CPU     = "#38bdf8"
    CHART_RAM     = "#a78bfa"
    CHART_DISK    = "#22c55e"
    CHART_NET     = "#f59e0b"
    CHART_HOST_CPU  = "#f59e0b"
    CHART_HOST_RAM  = "#22c55e"
    CHART_HOST_DISK = "#3b82f6"

    # Credential type colors
    CRED_PASSWORD = "#f59e0b"
    CRED_SSH_KEY  = "#8b5cf6"
    CRED_API_KEY  = "#22c55e"
    CRED_QMP_PASS = "#3b82f6"
    CRED_OTHER    = "#94a3b8"

    # ── Spacing (4px base unit) ────────────────────────────────────────────────

    XS = 4
    SM = 8
    MD = 12
    LG = 16
    XL = 24
    XXL = 32

    # ── Border Radius ──────────────────────────────────────────────────────────

    R_SM = 4
    R_MD = 6
    R_LG = 8
    R_XL = 12
    R_FULL = 9999

    # ── Typography ─────────────────────────────────────────────────────────────

    FS_XS  = 10
    FS_SM  = 11
    FS_MD  = 12
    FS_LG  = 13
    FS_XL  = 14
    FS_XXL = 16

    # ── Shadows ────────────────────────────────────────────────────────────────

    SHADOW_SM = "0 1px 3px rgba(0,0,0,0.3)"
    SHADOW_MD = "0 2px 8px rgba(0,0,0,0.4)"
    SHADOW_LG = "0 4px 16px rgba(0,0,0,0.5)"

    # ── Transitions ────────────────────────────────────────────────────────────

    TRANS_FAST   = 100
    TRANS_NORMAL = 200
    TRANS_SLOW   = 300


# ═══════════════════════════════════════════════════════════════════════════════
# QSS Generators
# ═══════════════════════════════════════════════════════════════════════════════

def dark_palette() -> QPalette:
    """Application QPalette for the dark theme."""
    p = QPalette()
    p.setColor(QPalette.Window, QColor(T.BG_PRIMARY))
    p.setColor(QPalette.WindowText, QColor(T.TEXT_PRIMARY))
    p.setColor(QPalette.Base, QColor(T.BG_PRIMARY))
    p.setColor(QPalette.AlternateBase, QColor(T.BG_SECONDARY))
    p.setColor(QPalette.Text, QColor(T.TEXT_PRIMARY))
    p.setColor(QPalette.Button, QColor(T.BG_SECONDARY))
    p.setColor(QPalette.ButtonText, QColor(T.TEXT_PRIMARY))
    p.setColor(QPalette.BrightText, QColor(T.ERROR))
    p.setColor(QPalette.Link, QColor(T.BRAND))
    p.setColor(QPalette.Highlight, QColor(T.BRAND))
    p.setColor(QPalette.HighlightedText, QColor(T.BG_PRIMARY))
    return p


# ── Widget Style Generators ─────────────────────────────────────────────────────

def card_style() -> str:
    """QFrame Card stylesheet.

    No box-shadow. Qt's stylesheet engine implements a subset of CSS 2.1 and has
    no box-shadow at all, so the property was never rendered -- it only made
    every Card in the window emit "Unknown property box-shadow" to the Qt log,
    which buried real warnings. Qt shadows are done with
    QGraphicsDropShadowEffect on the widget, not from a stylesheet.
    """
    return (
        "background: " + T.BG_SECONDARY + ";"
        "border: 1px solid " + T.BG_TERTIARY + ";"
        "border-radius: " + str(T.R_LG) + "px;"
    )


def card_title_style() -> str:
    """Card title label stylesheet."""
    return (
        "color: " + T.TEXT_SECONDARY + ";"
        "font-size: " + str(T.FS_XS) + "px;"
        "font-weight: 600;"
        "text-transform: uppercase;"
        "letter-spacing: 0.5px;"
    )


def primary_label_style() -> str:
    """Primary text label (values, names)."""
    return (
        "color: " + T.TEXT_PRIMARY + ";"
        "font-size: " + str(T.FS_LG) + "px;"
        "font-weight: 600;"
    )


def secondary_label_style() -> str:
    """Secondary text label."""
    return (
        "color: " + T.TEXT_SECONDARY + ";"
        "font-size: " + str(T.FS_MD) + "px;"
    )


def muted_label_style() -> str:
    """Muted text label."""
    return (
        "color: " + T.TEXT_MUTED + ";"
        "font-size: " + str(T.FS_MD) + "px;"
    )


def input_style() -> str:
    """Text input stylesheet."""
    return (
        "background: " + T.BG_PRIMARY + ";"
        "border: 1px solid " + T.BG_TERTIARY + ";"
        "border-radius: " + str(T.R_SM) + "px;"
        "color: " + T.TEXT_PRIMARY + ";"
        "padding: 6px 10px;"
        "font-size: " + str(T.FS_LG) + "px;"
        ""
        ":focus {"
        "  border-color: " + T.BRAND + ";"
        "  background: " + T.BG_SECONDARY + ";"
        "}"
    )


def button_green_style() -> str:
    """Primary action button (green)."""
    return (
        "background: " + T.SUCCESS + ";"
        "color: white;"
        "border: none;"
        "border-radius: " + str(T.R_SM) + "px;"
        "font-size: " + str(T.FS_LG) + "px;"
        "font-weight: 600;"
        "padding: 0 16px;"
        "min-height: 32px;"
        ""
        ":hover { background: #16a34a; }"
        ":disabled {"
        "  background: " + T.SUCCESS_BG + ";"
        "  color: " + T.TEXT_MUTED + ";"
        "}"
    )


def button_red_style() -> str:
    """Danger button (red)."""
    return (
        "background: " + T.ERROR + ";"
        "color: white;"
        "border: none;"
        "border-radius: " + str(T.R_SM) + "px;"
        "font-size: " + str(T.FS_LG) + "px;"
        "font-weight: 600;"
        "padding: 0 16px;"
        "min-height: 32px;"
        ""
        ":hover { background: #dc2626; }"
        ":disabled {"
        "  background: " + T.ERROR_BG + ";"
        "  color: " + T.TEXT_MUTED + ";"
        "}"
    )


def button_blue_style() -> str:
    """Secondary button (blue)."""
    return (
        "background: " + T.BRAND + ";"
        "color: white;"
        "border: none;"
        "border-radius: " + str(T.R_SM) + "px;"
        "font-size: " + str(T.FS_LG) + "px;"
        "font-weight: 600;"
        "padding: 0 16px;"
        "min-height: 32px;"
        ""
        ":hover { background: " + T.BRAND_HOVER + "; }"
        ":disabled {"
        "  background: " + T.BRAND + "20;"
        "  color: " + T.TEXT_MUTED + ";"
        "}"
    )


def button_ghost_style() -> str:
    """Ghost/transparent button."""
    return (
        "background: transparent;"
        "color: " + T.TEXT_SECONDARY + ";"
        "border: none;"
        "font-size: " + str(T.FS_MD) + "px;"
        "padding: 0 8px;"
        ""
        ":hover { color: " + T.TEXT_PRIMARY + "; }"
        ":pressed { color: " + T.TEXT_PRIMARY + "; }"
    )


def button_bordered_style() -> str:
    """Bordered button (secondary actions)."""
    return (
        "background: transparent;"
        "color: " + T.TEXT_SECONDARY + ";"
        "border: 1px solid " + T.BG_TERTIARY + ";"
        "border-radius: " + str(T.R_SM) + "px;"
        "font-size: " + str(T.FS_MD) + "px;"
        "padding: 0 12px;"
        "min-height: 28px;"
        ""
        ":hover {"
        "  color: " + T.TEXT_PRIMARY + ";"
        "  border-color: " + T.BRAND + ";"
        "}"
    )


def lifecycle_btn_style(color: str) -> str:
    """Lifecycle control button with custom color."""
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    darker = f"#{max(0,int(r*0.85)):02x}{max(0,int(g*0.85)):02x}{max(0,int(b*0.85)):02x}"
    return (
        "background: " + color + ";"
        "color: white;"
        "border: none;"
        "border-radius: " + str(T.R_MD) + "px;"
        "font-size: " + str(T.FS_LG) + "px;"
        "font-weight: 600;"
        "padding: 0 12px;"
        "min-height: 40px;"
        "min-width: 80px;"
        ""
        ":hover { background: " + darker + "; }"
        ":disabled {"
        "  background: " + color + "20;"
        "  color: " + T.TEXT_MUTED + ";"
        "}"
    )


def sidebar_btn_style(selector: str = "") -> str:
    """Sidebar navigation button stylesheet."""
    sel = ("#" + selector) if selector else ""
    return (
        "background: transparent;"
        "color: " + T.TEXT_SECONDARY + ";"
        "border: none;"
        "border-radius: " + str(T.R_MD) + "px;"
        "font-size: " + str(T.FS_LG) + "px;"
        "padding: 0 8px;"
        "min-height: 36px;"
        ""
        ":hover {"
        "  background: " + T.BG_SECONDARY + ";"
        "  color: " + T.TEXT_PRIMARY + ";"
        "}"
        ":checked {"
        "  background: #1e3a5f;"
        "  color: " + T.BRAND + ";"
        "}"
        + sel + ":hover {"
        "  background: " + T.BG_SECONDARY + ";"
        "  color: " + T.TEXT_PRIMARY + ";"
        "}"
    )


def status_bar_style() -> str:
    """QStatusBar stylesheet."""
    return (
        "background: " + T.BG_PRIMARY + ";"
        "color: " + T.TEXT_SECONDARY + ";"
        "border-top: 1px solid " + T.BG_TERTIARY + ";"
        "font-size: " + str(T.FS_MD) + "px;"
        "padding: 2px 8px;"
    )


def tab_bar_style() -> str:
    """QTabWidget tab bar stylesheet."""
    return (
        "QTabWidget::pane {"
        "  background: " + T.BG_PRIMARY + ";"
        "  border: 1px solid " + T.BG_TERTIARY + ";"
        "  border-radius: " + str(T.R_LG) + "px;"
        "}"
        "QTabBar::tab {"
        "  background: " + T.BG_SECONDARY + ";"
        "  color: " + T.TEXT_MUTED + ";"
        "  border: 1px solid " + T.BG_TERTIARY + ";"
        "  border-bottom: none;"
        "  padding: 8px 16px;"
        "  font-size: " + str(T.FS_MD) + "px;"
        "  min-width: 80px;"
        "  border-radius: " + str(T.R_LG) + "px " + str(T.R_LG) + "px 0 0;"
        "}"
        "QTabBar::tab:selected {"
        "  background: #1e3a5f;"
        "  color: " + T.BRAND + ";"
        "  border-color: " + T.BRAND + ";"
        "}"
        "QTabBar::tab:!selected:hover {"
        "  background: " + T.BG_SECONDARY + ";"
        "  color: " + T.TEXT_SECONDARY + ";"
        "}"
    )


def tree_style() -> str:
    """QTreeWidget stylesheet."""
    return (
        "background: " + T.BG_PRIMARY + ";"
        "color: " + T.TEXT_PRIMARY + ";"
        "border: 1px solid " + T.BG_TERTIARY + ";"
        "border-radius: " + str(T.R_SM) + "px;"
        "font-size: " + str(T.FS_MD) + "px;"
        "padding: 4px;"
        ""
        "QTreeWidget::item { padding: 4px 6px; }"
        "QTreeWidget::item:selected {"
        "  background: #1e3a5f;"
        "  color: " + T.BRAND + ";"
        "}"
        "QTreeWidget::item:hover {"
        "  background: " + T.BG_SECONDARY + ";"
        "}"
        "QHeaderView::section {"
        "  background: " + T.BG_SECONDARY + ";"
        "  color: " + T.TEXT_SECONDARY + ";"
        "  border: 1px solid " + T.BG_TERTIARY + ";"
        "  padding: 4px;"
        "  font-size: " + str(T.FS_SM) + "px;"
        "  font-weight: bold;"
        "}"
    )


def list_style() -> str:
    """QListWidget stylesheet."""
    return (
        "background: " + T.BG_PRIMARY + ";"
        "color: " + T.TEXT_PRIMARY + ";"
        "border: none;"
        "font-size: " + str(T.FS_MD) + "px;"
        "padding: 4px;"
        ""
        "QListWidget::item {"
        "  padding: 4px 8px;"
        "  border-bottom: 1px solid " + T.BG_TERTIARY + ";"
        "}"
        "QListWidget::item:selected {"
        "  background: #1e3a5f;"
        "  color: " + T.BRAND + ";"
        "}"
    )


def progress_style() -> str:
    """QProgressBar stylesheet."""
    return (
        "background: " + T.BG_SECONDARY + ";"
        "border: none;"
        "border-radius: " + str(T.R_SM) + "px;"
        "height: 4px;"
        "text-align: center;"
        ""
        "QProgressBar::chunk {"
        "  background: " + T.BRAND + ";"
        "  border-radius: " + str(T.R_SM) + "px;"
        "}"
    )


def combo_style() -> str:
    """QComboBox stylesheet."""
    return (
        "background: " + T.BG_PRIMARY + ";"
        "color: " + T.TEXT_PRIMARY + ";"
        "border: 1px solid " + T.BG_TERTIARY + ";"
        "border-radius: " + str(T.R_SM) + "px;"
        "padding: 4px 8px;"
        "font-size: " + str(T.FS_MD) + "px;"
        "min-height: 28px;"
        ""
        ":hover { border-color: " + T.BRAND + "; }"
        "QComboBox::drop-down { border: none; }"
        "QComboBox::down-arrow { image: none; }"
    )


def spinbox_style() -> str:
    """QSpinBox stylesheet."""
    return (
        "background: " + T.BG_PRIMARY + ";"
        "color: " + T.TEXT_PRIMARY + ";"
        "border: 1px solid " + T.BG_TERTIARY + ";"
        "border-radius: " + str(T.R_SM) + "px;"
        "padding: 4px 8px;"
        "font-size: " + str(T.FS_MD) + "px;"
        "min-height: 28px;"
        ""
        ":hover { border-color: " + T.BRAND + "; }"
    )


def checkbox_style() -> str:
    """QCheckBox stylesheet."""
    return (
        "QCheckBox {"
        "  color: " + T.TEXT_PRIMARY + ";"
        "  font-size: " + str(T.FS_MD) + "px;"
        "}"
        "QCheckBox::indicator {"
        "  width: 14px;"
        "  height: 14px;"
        "  border: 1px solid " + T.BG_TERTIARY + ";"
        "  border-radius: 3px;"
        "  background: " + T.BG_PRIMARY + ";"
        "}"
        "QCheckBox::indicator:checked {"
        "  background: " + T.BRAND + ";"
        "  border-color: " + T.BRAND + ";"
        "}"
    )


def text_browser_style() -> str:
    """QTextBrowser / QTextEdit stylesheet (monospace)."""
    return (
        "background: " + T.BG_PRIMARY + ";"
        "color: " + T.TEXT_PRIMARY + ";"
        "border: none;"
        "border: 1px solid " + T.BG_TERTIARY + ";"
        "border-radius: " + str(T.R_SM) + "px;"
        "font-family: 'Consolas', 'Courier New', monospace;"
        "font-size: " + str(T.FS_MD) + "px;"
        "padding: 8px;"
    )


def dialog_style() -> str:
    """QDialog base stylesheet."""
    return (
        "QDialog { background: " + T.BG_PRIMARY + "; }"
        "QMessageBox {"
        "  background: " + T.BG_PRIMARY + ";"
        "  color: " + T.TEXT_PRIMARY + ";"
        "}"
        "QMessageBox QLabel {"
        "  color: " + T.TEXT_PRIMARY + ";"
        "  font-size: " + str(T.FS_LG) + "px;"
        "}"
        "QMessageBox QPushButton {"
        "  background: " + T.BG_SECONDARY + ";"
        "  color: " + T.TEXT_PRIMARY + ";"
        "  border: 1px solid " + T.BG_TERTIARY + ";"
        "  border-radius: " + str(T.R_SM) + "px;"
        "  padding: 6px 16px;"
        "  font-size: " + str(T.FS_MD) + "px;"
        "}"
        "QMessageBox QPushButton:hover { background: " + T.BG_TERTIARY + "; }"
    )


def splitter_style() -> str:
    """QSplitter stylesheet."""
    return (
        "QSplitter {"
        "  background: " + T.BG_PRIMARY + ";"
        "  color: " + T.TEXT_PRIMARY + ";"
        "}"
        "QSplitter::handle {"
        "  background: " + T.BG_TERTIARY + ";"
        "  border: none;"
        "}"
        "QSplitter::handle:hover {"
        "  background: " + T.BRAND + ";"
        "}"
    )


def title_bar_style() -> str:
    """Title bar QWidget stylesheet."""
    return (
        "background: " + T.BG_PRIMARY + ";"
        "border-bottom: 1px solid " + T.BG_TERTIARY + ";"
    )


def sidebar_style() -> str:
    """Sidebar QWidget stylesheet."""
    return (
        "background: " + T.BG_PRIMARY + ";"
        "border-right: 1px solid " + T.BG_TERTIARY + ";"
    )


def panel_bg_style() -> str:
    """Standard panel background."""
    return "background: " + T.BG_PRIMARY + ";"
