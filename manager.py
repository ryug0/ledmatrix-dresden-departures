"""
Dresden VVO departure board plugin for ChuckBuilds LEDMatrix.

Layout (128×64): left panel = station + tram departures; right sidebar =
date, clock, current weather, and three forecast columns (next 6h / 13h / 20h).

Data sources (no API keys):
  - Departures: VVO WebAPI — pointfinder (stop lookup) + dm (departure monitor)
  - Weather:    Open-Meteo forecast API

Lifecycle (called by LEDMatrix):
  1. update()  — fetch departures + weather, store in cache (~15 s / ~10 min)
  2. display() — compose a PIL image; skip redraw if minute/data unchanged

Optional runtime deps (bundled with most LEDMatrix installs):
  - freetype-py — BDF bitmap fonts (required for readable text)
  - pyproj      — convert VVO stop coordinates to WGS84 for local weather

Developed with Cursor Composer 2.5 and hardware-tested.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from threading import Lock
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from src.plugin_system.base_plugin import BasePlugin

try:
    import freetype
except ImportError:
    freetype = None  # type: ignore[assignment]

try:
    import pyproj
except ImportError:
    pyproj = None  # type: ignore[assignment]

VVO_BASE_URL = "https://webapi.vvo-online.de"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

COLOR_BLACK = (0, 0, 0)
COLOR_ORANGE = (255, 141, 0)
COLOR_WHITE = (255, 255, 255)

# --- Display layout (left panel) ---

ROW_HEIGHT = 8
STATION_X = 2
STATION_DIVIDER_GAP = 0   # keep station text off the sidebar divider
STATION_SPACE_TRACKING = -3  # tighten gaps between words (6x13 space is 6px wide)
STATION_Y = 0
FIRST_ROW_Y_WITH_HEADER = 15
FIRST_ROW_Y_NO_HEADER = 0
DEST_NAME_X = 15          # destination text start
MINS_RIGHT_PAD = 1        # gap before countdown
MINS_COLUMN_CHARS = 2     # fixed width reserve (99 min) so truncation dot stays aligned
DIRECTION_ARROW = ">"     # single-char separator on the LED panel (VVO uses "->")

# Fallback when schema merge did not run; keep in sync with config_schema.json "sidebar_percent.default"
_SIDEBAR_PERCENT_DEFAULT = 40

# --- Sidebar layout (edit y-values here to tune the right panel) ---

SIDEBAR_LAYOUT = {
    "date_y": 1,
    "date_gap": 3,           # pixels between weekday ("Mi") and "3."
    "date_month_gap": 2,     # pixels between day dot ("25.") and month ("Jul")
    "time_y": 9,
    "time_tracking": 0,
    "time_digit_tracking": 1,  # 1px gap between adjacent digits (e.g. 08)
    "time_colon_tracking": 0,  # extra px after each clock colon
    "weather_icon_y": 23,
    "weather_temp_y": 25,
    "weather_icon_size": 12,
    "forecast_labels_y": 36,   # hour labels
    "forecast_icons_y": 45,    # icon above temp in each column
    "forecast_temps_y": 56,    # +2px gap below forecast icons (sun rays)
    "forecast_icon_size": 8,
    "forecast_col_gutter": 0,  # px gap between time columns
    "forecast_temp_offset_x": 2,  # nudge temps right to align under hour labels
}

_GK4_TO_WGS: Any = None

DAY_NAMES_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONTH_NAMES_DE = [
    "Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
    "Jul", "Aug", "Sep", "Okt", "Nov", "Dez",
]

DATE_RE = re.compile(r"/Date\((\d+)")

# --- Open-Meteo WMO weather code groups ---

WeatherIcon = Literal[
    "sun", "partly_cloudy", "cloud", "drizzle", "rain_light", "rain_heavy",
    "snow", "ice", "fog", "storm",
]

DRIZZLE_CODES = {51, 52, 53, 54, 55}
RAIN_LIGHT_CODES = {61, 63, 80, 81}
RAIN_HEAVY_CODES = {65, 82}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
ICE_CODES = {56, 57, 66, 67}
FOG_CODES = {45, 48}
PARTLY_CLOUDY_CODES = {2}
OVERCAST_CODES = {3}
CLEAR_CODES = {0, 1}
STORM_CODES = {95, 96, 99}

FORECAST_ANCHOR_HOURS = (6, 13, 20)
FORECAST_SLOT_COUNT = 3
FORECAST_COL_GUTTER = 3
WEATHER_CACHE_VERSION = 3
CONFIG_RELOAD_INTERVAL_S = 30
VVO_TRIP_TIMEOUT_S = 5
VVO_TRIP_WORKERS = 4


def _gk4_to_wgs(gk_lat: int, gk_lng: int) -> Optional[Tuple[float, float]]:
    """Convert VVO GK4 integer coords (pipe fields 4/5) to WGS84 lat/lon."""
    global _GK4_TO_WGS
    if pyproj is None:
        return None
    if _GK4_TO_WGS is None:
        _GK4_TO_WGS = pyproj.Transformer.from_crs("EPSG:5678", "EPSG:4326", always_xy=True)
    wgs_lng, wgs_lat = _GK4_TO_WGS.transform(gk_lng, gk_lat)
    return wgs_lat, wgs_lng


def _find_ledmatrix_root() -> Path:
    """Locate LEDMatrix install (contains assets/fonts)."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / "assets" / "fonts").is_dir():
            return current
        current = current.parent
    raise RuntimeError("Could not find LEDMatrix assets/fonts directory")


def _ledmatrix_fonts_dir(plugin_manager: Any) -> Path:
    """Resolve assets/fonts via plugin manager (works for symlinked external repos)."""
    if plugin_manager is not None and hasattr(plugin_manager, "plugins_dir"):
        root = Path(plugin_manager.plugins_dir).resolve().parent
        fonts = root / "assets" / "fonts"
        if fonts.is_dir():
            return fonts
    return _find_ledmatrix_root() / "assets" / "fonts"


class BdfFont:
    """Crisp bitmap font for low-resolution LED panels (via freetype-py)."""

    def __init__(self, path: Path, line_height: int) -> None:
        if freetype is None:
            raise RuntimeError("freetype-py is required for BDF fonts")
        self._face = freetype.Face(str(path))
        self.line_height = line_height
        self._ascender = self._face.size.ascender >> 6
        if self._ascender <= 0:
            self._face.load_char("H")
            self._ascender = max(int(self._face.glyph.bitmap_top), line_height - 1)

    def text_width(
        self,
        text: str,
        tracking: int = 0,
        space_tracking: int = 0,
        digit_tracking: int = 0,
        small_colon: bool = False,
        colon_tracking: int = 0,
    ) -> int:
        width = 0
        for i, char in enumerate(text):
            if char == ".":
                width += self._PERIOD_ADVANCE
            elif char == ":" and small_colon:
                width += self._COLON_ADVANCE
                if colon_tracking and i < len(text) - 1:
                    width += colon_tracking
            else:
                self._face.load_char(char)
                width += self._face.glyph.advance.x >> 6
            if char == " " and space_tracking:
                width += space_tracking
            elif i < len(text) - 1:
                next_char = text[i + 1]
                if digit_tracking and char.isdigit() and next_char.isdigit():
                    width += digit_tracking
                elif tracking:
                    width += tracking
        return width

    _PERIOD_ADVANCE = 2  # BDF "." is a 2×2 block; we render one pixel in a narrow cell
    _COLON_ADVANCE = 4   # clock colon: two 2×2 dots, tight cell

    def _fill_2x2(
        self,
        draw: ImageDraw.ImageDraw,
        cx: int,
        cy: int,
        color: Tuple[int, int, int],
    ) -> None:
        for dy in (0, 1):
            for dx in (0, 1):
                draw.point((cx + dx, cy + dy), fill=color)

    def _draw_period_dot(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        baseline_y: int,
        color: Tuple[int, int, int],
    ) -> int:
        draw.point((x + self._PERIOD_ADVANCE // 2, baseline_y - 1), fill=color)
        return self._PERIOD_ADVANCE

    def _draw_small_colon(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        baseline_y: int,
        color: Tuple[int, int, int],
    ) -> int:
        cx = x + (self._COLON_ADVANCE - 2) // 2
        self._fill_2x2(draw, cx, baseline_y - 6, color)
        self._fill_2x2(draw, cx, baseline_y - 3, color)
        return self._COLON_ADVANCE

    def draw(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        x: int,
        y: int,
        color: Tuple[int, int, int],
        tracking: int = 0,
        space_tracking: int = 0,
        digit_tracking: int = 0,
        small_colon: bool = False,
        colon_tracking: int = 0,
    ) -> None:
        baseline_y = y + self._ascender
        for i, char in enumerate(text):
            if char == ".":
                x += self._draw_period_dot(draw, x, baseline_y, color)
            elif char == ":" and small_colon:
                x += self._draw_small_colon(draw, x, baseline_y, color)
                if colon_tracking and i < len(text) - 1:
                    x += colon_tracking
            else:
                self._face.load_char(char)
                bitmap = self._face.glyph.bitmap
                glyph_left = self._face.glyph.bitmap_left
                glyph_top = self._face.glyph.bitmap_top
                for row in range(bitmap.rows):
                    for col in range(bitmap.width):
                        byte_index = row * bitmap.pitch + (col // 8)
                        if byte_index < len(bitmap.buffer):
                            byte = bitmap.buffer[byte_index]
                            if byte & (1 << (7 - (col % 8))):
                                draw.point((x + glyph_left + col, baseline_y - glyph_top + row), fill=color)
                x += self._face.glyph.advance.x >> 6
            if char == " " and space_tracking:
                x += space_tracking
            elif i < len(text) - 1:
                next_char = text[i + 1]
                if digit_tracking and char.isdigit() and next_char.isdigit():
                    x += digit_tracking
                elif tracking:
                    x += tracking


class DresdenDeparturesPlugin(BasePlugin):
    """Dresden VVO departure board with platform-style split layout."""

    # --- Config & setup ---

    def __init__(
        self,
        plugin_id: str,
        config: Dict[str, Any],
        display_manager,
        cache_manager,
        plugin_manager,
    ) -> None:
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)
        self.stop_query = str(config.get("stop_id", "")).strip()
        self.stop_name_override = str(config.get("stop_name", "")).strip()
        self.tram_only = bool(config.get("tram_only", True))
        self.max_departures = int(config.get("max_departures", 6))
        self.show_station_header = bool(config.get("show_station_header", True))
        self.timezone = ZoneInfo(config.get("timezone", "Europe/Berlin"))
        self.weather_at_stop = bool(config.get("weather_at_stop", True))
        self._config_latitude = float(config.get("latitude", 51.0504))
        self._config_longitude = float(config.get("longitude", 13.7373))
        self._stop_latitude: Optional[float] = None
        self._stop_longitude: Optional[float] = None
        self.sidebar_percent = float(config.get("sidebar_percent", _SIDEBAR_PERCENT_DEFAULT))
        self._destination_aliases = self._parse_destination_aliases(config.get("destination_aliases"))
        self._hidden_lines = self._parse_hidden_lines(config.get("hidden_lines"))
        self._config_sig = self._config_signature(config)

        self._station_name = self.stop_name_override or "Haltestelle"
        self._departures: List[Dict[str, Any]] = []
        self._weather: Dict[str, Any] = {}
        self._last_render_key: Optional[str] = None
        self._cached_image: Optional[Image.Image] = None
        self._vvo_stop_id: Optional[str] = None
        self._vvo_stop_query: Optional[str] = None
        self._config_reload_at: float = 0.0
        self._update_lock = Lock()

        fonts_dir = _ledmatrix_fonts_dir(plugin_manager)
        self._font_body = self._load_bdf(fonts_dir / "5x7.bdf", 8)
        self._font_station = self._load_bdf(fonts_dir / "6x13.bdf", 13)
        self._font_header = self._load_bdf(fonts_dir / "6x13B.bdf", 13)

    @staticmethod
    def _parse_destination_aliases(raw: Any) -> Dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        aliases: Dict[str, str] = {}
        for key, value in raw.items():
            src = str(key).strip()
            dst = str(value).strip()
            if src and dst:
                aliases[src] = dst
        return aliases

    @staticmethod
    def _parse_hidden_lines(raw: Any) -> set[str]:
        if not isinstance(raw, list):
            return set()
        return {str(line).strip() for line in raw if str(line).strip()}

    @staticmethod
    def _config_signature(config: Dict[str, Any]) -> str:
        aliases = config.get("destination_aliases") or {}
        aliases_sig = json.dumps(aliases, sort_keys=True, ensure_ascii=False) if isinstance(aliases, dict) else ""
        hidden = config.get("hidden_lines") or []
        hidden_sig = json.dumps(hidden, sort_keys=True) if isinstance(hidden, list) else ""
        return "|".join([
            str(config.get("stop_id", "")).strip(),
            str(config.get("stop_name", "")).strip(),
            str(config.get("weather_at_stop", True)),
            str(config.get("latitude", "")),
            str(config.get("longitude", "")),
            str(config.get("sidebar_percent", _SIDEBAR_PERCENT_DEFAULT)),
            str(config.get("max_departures", 6)),
            str(config.get("show_station_header", True)),
            aliases_sig,
            hidden_sig,
        ])

    def _reload_config(self) -> None:
        """Pick up config edits without reading disk on every display frame."""
        now = time.monotonic()
        if now - self._config_reload_at < CONFIG_RELOAD_INTERVAL_S:
            return
        self._config_reload_at = now
        pm = self.plugin_manager
        if pm is None:
            return
        cm = getattr(pm, "config_manager", None)
        if cm is None:
            return
        try:
            merged = cm.load_config()
            section = merged.get(self.plugin_id)
            if isinstance(section, dict):
                self.config = section
        except Exception as exc:
            self.logger.debug("Config reload skipped: %s", exc)

    def _sync_config(self) -> None:
        self._reload_config()
        new_stop = str(self.config.get("stop_id", "")).strip()
        if new_stop != self.stop_query:
            self._vvo_stop_id = None
            self._vvo_stop_query = None
        self.stop_query = new_stop
        self.stop_name_override = str(self.config.get("stop_name", "")).strip()
        self.tram_only = bool(self.config.get("tram_only", True))
        self.max_departures = int(self.config.get("max_departures", 6))
        self.show_station_header = bool(self.config.get("show_station_header", True))
        self.weather_at_stop = bool(self.config.get("weather_at_stop", True))
        self._config_latitude = float(self.config.get("latitude", 51.0504))
        self._config_longitude = float(self.config.get("longitude", 13.7373))
        self.sidebar_percent = float(self.config.get("sidebar_percent", _SIDEBAR_PERCENT_DEFAULT))
        self._destination_aliases = self._parse_destination_aliases(self.config.get("destination_aliases"))
        self._hidden_lines = self._parse_hidden_lines(self.config.get("hidden_lines"))
        sig = self._config_signature(self.config)
        if sig != self._config_sig:
            self._config_sig = sig
            self._cached_image = None
            self._last_render_key = None

    def _load_bdf(self, path: Path, line_height: int) -> BdfFont:
        if path.exists():
            try:
                return BdfFont(path, line_height)
            except Exception as exc:
                self.logger.warning("Could not load %s: %s", path.name, exc)
        fallback = _ledmatrix_fonts_dir(self.plugin_manager) / "6x13.bdf"
        return BdfFont(fallback, 13)

    # --- Data fetching (update) ---

    def update(self) -> None:
        """Refresh departure and weather data from VVO / Open-Meteo."""
        if not self._update_lock.acquire(blocking=False):
            return
        try:
            self._update_impl()
        finally:
            self._update_lock.release()

    def _update_impl(self) -> None:
        self._sync_config()
        if not self.stop_query:
            self.logger.warning("stop_id not configured")
            return

        refresh = max(5, int(self.config.get("update_interval", 15)))
        cache_key = f"{self.plugin_id}_dep_{self.stop_query}_v2"
        now = datetime.now(self.timezone)
        force_fetch = self._departures_are_stale(now)
        cached = None if force_fetch else self.cache_manager.get(cache_key, max_age=refresh)
        if force_fetch and self._departures:
            self.logger.info("Departures stale (all due); forcing VVO refresh")
        if cached:
            self._station_name = self.stop_name_override or cached.get("station_name", self._station_name)
            self._departures = cached.get("departures", [])
            self._stop_latitude = cached.get("latitude")
            self._stop_longitude = cached.get("longitude")
        else:
            try:
                station_name, departures, stop_lat, stop_lon = self._fetch_departures()
                self._station_name = self.stop_name_override or station_name
                self._departures = departures
                self._stop_latitude = stop_lat
                self._stop_longitude = stop_lon
                self.cache_manager.set(
                    cache_key,
                    {
                        "station_name": station_name,
                        "departures": self._departures,
                        "latitude": stop_lat,
                        "longitude": stop_lon,
                    },
                )
            except Exception as exc:
                self.logger.error("Departure fetch failed: %s", exc)

        wx_lat, wx_lon = self._weather_coordinates()
        weather_key = f"{self.plugin_id}_wx_v{WEATHER_CACHE_VERSION}_{wx_lat:.4f}_{wx_lon:.4f}"
        weather_cached = self.cache_manager.get(weather_key, max_age=600)
        if weather_cached and not isinstance(weather_cached.get("hourly"), dict):
            weather_cached = None
        if weather_cached:
            self._weather = weather_cached
        else:
            try:
                self._weather = self._fetch_weather(wx_lat, wx_lon)
                self.cache_manager.set(weather_key, self._weather)
            except Exception as exc:
                self.logger.error("Weather fetch failed: %s", exc)

    # --- Rendering (display) ---

    def _layout(self, width: int) -> Tuple[int, int, int]:
        right_w = max(18, int(width * self.sidebar_percent / 100))
        left_w = width - right_w - 1
        divider_x = left_w
        return left_w, divider_x, right_w

    def _first_row_y(self) -> int:
        return FIRST_ROW_Y_WITH_HEADER if self.show_station_header else FIRST_ROW_Y_NO_HEADER

    def _visible_departure_count(self, height: int) -> int:
        rows_fit = max(1, (height - self._first_row_y()) // ROW_HEIGHT)
        return min(self.max_departures, rows_fit)

    def display(self, force_clear: bool = False) -> None:
        """Draw the departure board; reuse cached frame when nothing changed."""
        self._sync_config()
        now = datetime.now(self.timezone)
        forecast_slots = self._hourly_forecast_slots(self._weather.get("hourly") or {}, now)
        weather_sig = f"{self._weather.get('temp')}|{forecast_slots}"
        render_key = (
            f"{now:%Y%m%d%H%M%S}|{self._departure_sig(now)}|{weather_sig}|"
            f"{self._station_name}|{self.stop_query}|{self.show_station_header}"
        )
        if not force_clear and render_key == self._last_render_key and self._cached_image is not None:
            self.display_manager.image = self._cached_image
            self.display_manager.update_display()
            return

        width = self.display_manager.width
        height = self.display_manager.height
        left_w, divider_x, right_w = self._layout(width)
        img = Image.new("RGB", (width, height), COLOR_BLACK)
        draw = ImageDraw.Draw(img)
        visible_rows = self._visible_departure_count(height)
        self._draw_left_panel(draw, left_w, height, visible_rows, now)
        draw.line([(divider_x, 0), (divider_x, height - 1)], fill=COLOR_ORANGE, width=1)
        self._draw_right_panel(draw, divider_x + 1, right_w, height, now, forecast_slots)

        self._cached_image = img
        self._last_render_key = render_key
        self.display_manager.image = img
        self.display_manager.update_display()

    def _departure_sig(self, now: datetime) -> str:
        return "|".join(
            f"{d['line']}:{self._minutes_until(d, now)}"
            for d in self._visible_departures(now)
        )

    def _departures_are_stale(self, now: datetime) -> bool:
        """True when every cached departure is due (UI would show 0 for all rows)."""
        if not self._departures:
            return False
        return all(self._minutes_until(dep, now) <= 0 for dep in self._departures)

    def _visible_departures(self, now: datetime) -> List[Dict[str, Any]]:
        """Apply hidden-line filter and row cap for display."""
        visible: List[Dict[str, Any]] = []
        for dep in self._departures:
            if dep.get("line", "").strip() in self._hidden_lines:
                continue
            visible.append(dep)
            if len(visible) >= self.max_departures:
                break
        return visible

    def _minutes_until(self, dep: Dict[str, Any], now: datetime) -> int:
        """Live countdown from stored departure time (updates every display tick).

        Floor minutes so the count never overshoots — tram tends to feel on time
        or a little early vs the number shown.
        """
        departs_at = dep.get("departs_at")
        if departs_at:
            try:
                when = datetime.fromisoformat(str(departs_at))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=self.timezone)
                else:
                    when = when.astimezone(self.timezone)
                seconds = max(0, int((when - now).total_seconds()))
                return seconds // 60
            except (TypeError, ValueError):
                pass
        return max(0, int(dep.get("minutes", 0)))

    def _display_station_name(self) -> str:
        if self.stop_name_override:
            return self.stop_name_override
        return self._station_name

    def _station_max_width(self, left_w: int) -> int:
        return max(8, left_w - STATION_X - STATION_DIVIDER_GAP)

    def _draw_left_panel(
        self,
        draw: ImageDraw.ImageDraw,
        left_w: int,
        height: int,
        visible_rows: int,
        now: datetime,
    ) -> None:
        row_y = self._first_row_y()
        if self.show_station_header:
            header = self._display_station_name()
            header_text = self._truncate(
                header,
                self._station_max_width(left_w),
                self._font_station,
                space_tracking=STATION_SPACE_TRACKING,
            )
            self._font_station.draw(
                draw, header_text, STATION_X, STATION_Y, COLOR_WHITE,
                space_tracking=STATION_SPACE_TRACKING,
            )

        shown = 0
        for dep in self._visible_departures(now):
            if shown >= visible_rows or row_y + ROW_HEIGHT > height:
                break
            self._font_body.draw(draw, dep["line"][:3], 2, row_y, COLOR_ORANGE)
            direction = self._display_direction(
                dep["direction"],
                dep.get("route_stops_ahead"),
            )
            mins = str(self._minutes_until(dep, now))
            max_dest_w = self._dest_name_max_width(left_w)
            self._font_body.draw(
                draw,
                self._truncate(direction, max_dest_w, self._font_body),
                DEST_NAME_X,
                row_y,
                COLOR_ORANGE,
            )
            self._font_body.draw(
                draw,
                mins,
                left_w - self._font_body.text_width(mins) - MINS_RIGHT_PAD,
                row_y,
                COLOR_ORANGE,
            )
            row_y += ROW_HEIGHT
            shown += 1

        if not self._departures:
            msg = "stop_id fehlt" if not self.stop_query else "Keine Daten"
            self._font_body.draw(draw, msg, 2, self._first_row_y(), COLOR_ORANGE)

    def _draw_right_panel(
        self,
        draw: ImageDraw.ImageDraw,
        x0: int,
        panel_w: int,
        height: int,
        now: datetime,
        forecast_slots: List[Dict[str, Any]],
    ) -> None:
        lay = SIDEBAR_LAYOUT
        pad = 1
        inner_w = panel_w - pad * 2
        cx = x0 + pad

        self._draw_date_line(
            draw, cx, inner_w, lay["date_y"], now,
            lay["date_gap"], lay.get("date_month_gap", 0),
        )

        time_str = now.strftime("%H:%M:%S")
        self._draw_centered_spaced_bdf(
            draw, cx, inner_w, lay["time_y"], time_str,
            self._font_header, COLOR_ORANGE,
            lay.get("time_tracking", 0),
            lay.get("time_digit_tracking", 0),
            small_colon=True,
            colon_tracking=lay.get("time_colon_tracking", 0),
        )

        icon = self._code_to_icon(int(self._weather.get("weather_code") or 0))
        temp = self._weather.get("temp")
        temp_str = f"{round(temp)}°" if temp is not None else "--°"
        icon_size = lay["weather_icon_size"]
        temp_w = self._font_body.text_width(temp_str)
        block_w = icon_size + 2 + temp_w
        icon_x = cx + max(0, (inner_w - block_w) // 2)
        self._draw_weather_icon(draw, icon_x, lay["weather_icon_y"], icon_size, icon)
        self._font_body.draw(draw, temp_str, icon_x + icon_size + 2, lay["weather_temp_y"], COLOR_ORANGE)

        if forecast_slots:
            self._draw_hourly_forecast(draw, cx, inner_w, lay, forecast_slots)

    # --- Sidebar widgets ---

    def _draw_date_line(
        self,
        draw: ImageDraw.ImageDraw,
        x0: int,
        width: int,
        y: int,
        now: datetime,
        gap_px: int,
        month_gap_px: int = 0,
    ) -> None:
        """Draw ``Mi`` + gap + ``3.`` + gap + ``Jun`` centered as one block."""
        font = self._font_body
        day_name = DAY_NAMES_DE[now.weekday()]
        day_num = f"{now.day}."
        month = MONTH_NAMES_DE[now.month - 1]
        total_w = (
            font.text_width(day_name) + gap_px
            + font.text_width(day_num) + month_gap_px + font.text_width(month)
        )
        start_x = x0 + max(0, round((width - total_w) / 2))
        font.draw(draw, day_name, start_x, y, COLOR_ORANGE)
        x = start_x + font.text_width(day_name) + gap_px
        font.draw(draw, day_num, x, y, COLOR_ORANGE)
        font.draw(draw, month, x + font.text_width(day_num) + month_gap_px, y, COLOR_ORANGE)

    def _draw_centered_spaced_bdf(
        self,
        draw: ImageDraw.ImageDraw,
        x0: int,
        width: int,
        y: int,
        text: str,
        font: BdfFont,
        color: Tuple[int, int, int],
        tracking: int = 0,
        digit_tracking: int = 0,
        small_colon: bool = False,
        colon_tracking: int = 0,
    ) -> None:
        text_w = font.text_width(
            text,
            tracking=tracking,
            digit_tracking=digit_tracking,
            small_colon=small_colon,
            colon_tracking=colon_tracking,
        )
        font.draw(
            draw, text, x0 + max(0, (width - text_w) // 2), y, color,
            tracking=tracking,
            digit_tracking=digit_tracking,
            small_colon=small_colon,
            colon_tracking=colon_tracking,
        )

    def _forecast_icon_xy(
        self,
        label_center: int,
        icon_size: int,
        icon: str,
        base_y: int,
    ) -> Tuple[int, int]:
        x = label_center - icon_size // 2
        return x, base_y

    def _format_forecast_temp(self, temp: Any) -> str:
        if temp is None:
            return "--°"
        try:
            return f"{int(round(float(temp)))}°"
        except (TypeError, ValueError):
            return "--°"

    def _draw_hourly_forecast(
        self,
        draw: ImageDraw.ImageDraw,
        x0: int,
        width: int,
        lay: Dict[str, Any],
        slots: List[Dict[str, Any]],
    ) -> None:
        """Equal columns with gutter; labels/temps clipped to column width."""
        count = len(slots)
        if count == 0:
            return
        gutter = lay.get("forecast_col_gutter", FORECAST_COL_GUTTER)
        icon_size = lay.get("forecast_icon_size", 8)
        total_gutter = gutter * max(0, count - 1)
        col_w = max(1, (width - total_gutter) // count)

        for idx, slot in enumerate(slots):
            col_x = x0 + idx * (col_w + gutter)
            hour = int(slot.get("hour", 0))
            label = f"{hour}h"
            label_w = self._font_body.text_width(label)
            label_x = col_x + max(0, (col_w - label_w) // 2)
            self._font_body.draw(draw, label, label_x, lay["forecast_labels_y"], COLOR_ORANGE)

            label_center = label_x + label_w // 2
            icon = str(slot.get("icon", "cloud"))
            icon_x, icon_y = self._forecast_icon_xy(
                label_center, icon_size, icon, lay["forecast_icons_y"],
            )
            self._draw_weather_icon(draw, icon_x, icon_y, icon_size, icon)
            temp_str = self._format_forecast_temp(slot.get("temp"))
            temp_w = self._font_body.text_width(temp_str)
            temp_offset = lay.get("forecast_temp_offset_x", 0)
            temp_x = label_center - temp_w // 2 + temp_offset
            self._font_body.draw(draw, temp_str, temp_x, lay["forecast_temps_y"], COLOR_ORANGE)

    # --- Weather icons (pixel art) ---

    def _draw_weather_icon(self, draw: ImageDraw.ImageDraw, x: int, y: int, size: int, icon: str = "cloud") -> None:
        if icon == "sun":
            self._icon_sun(draw, x, y, size)
        elif icon == "partly_cloudy":
            self._icon_partly_cloudy(draw, x, y, size)
        elif icon == "drizzle":
            self._icon_drizzle(draw, x, y, size)
        elif icon in ("rain", "rain_light"):
            self._icon_rain_light(draw, x, y, size)
        elif icon == "rain_heavy":
            self._icon_rain_heavy(draw, x, y, size)
        elif icon == "snow":
            self._icon_snow(draw, x, y, size)
        elif icon == "ice":
            self._icon_ice(draw, x, y, size)
        elif icon == "fog":
            self._icon_fog(draw, x, y, size)
        elif icon == "storm":
            self._icon_storm(draw, x, y, size)
        else:
            self._icon_cloud(draw, x, y, size)

    def _icon_sun(self, draw: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
        cx, cy = x + s // 2, y + s // 2
        r = max(2, s // 4)
        ray_end = max(r + 2, (s - 1) // 2)
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            draw.line(
                [(cx + dx * (r + 1), cy + dy * (r + 1)), (cx + dx * ray_end, cy + dy * ray_end)],
                fill=COLOR_ORANGE,
                width=1,
            )
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=COLOR_ORANGE)

    def _icon_partly_cloudy(self, draw: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
        """Partly cloudy — forecast slots reuse the main-panel cloud icon."""
        if s <= 9:
            self._icon_cloud(draw, x, y, s)
            return
        cx, cy = x + max(2, s // 4), y + s // 3
        r = max(2, s // 4)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=COLOR_ORANGE)
        draw.line([(cx, cy), (cx - r, cy - r - 1)], fill=COLOR_ORANGE, width=1)
        draw.line([(cx, cy), (cx - r - 1, cy)], fill=COLOR_ORANGE, width=1)
        draw.ellipse([x + s // 3, y + s // 3, x + s * 3 // 4, y + s * 2 // 3], outline=COLOR_ORANGE)
        draw.ellipse([x + s // 2, y + s // 4, x + s - 1, y + s * 2 // 3], outline=COLOR_ORANGE)

    def _icon_cloud(self, draw: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
        """Two overlapping circle outlines — centered in forecast slots, classic layout at 12px+."""
        if s <= 9:
            cx, cy = x + s // 2, y + s // 2
            r = max(2, s // 4)
            draw.ellipse([cx - r - 1, cy - 1, cx, cy + r], outline=COLOR_ORANGE)
            draw.ellipse([cx - 1, cy - r, cx + r + 1, cy + r], outline=COLOR_ORANGE)
            return
        draw.ellipse([x + s // 8, y + s // 3, x + s // 2, y + s * 2 // 3], outline=COLOR_ORANGE)
        draw.ellipse([x + s // 3, y + s // 5, x + s * 7 // 8, y + s * 2 // 3], outline=COLOR_ORANGE)

    def _plot_icon_pixels(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        s: int,
        w: int,
        h: int,
        pixels: Tuple[Tuple[int, int], ...],
    ) -> None:
        """Draw a fixed pixel sprite centered inside an s×s box."""
        ox = x + max(0, (s - w) // 2)
        oy = y + max(0, (s - h) // 2)
        for px, py in pixels:
            draw.point((ox + px, oy + py), fill=COLOR_ORANGE)

    def _icon_rain_small(self, draw: ImageDraw.ImageDraw, x: int, y: int, s: int, drops: int = 2) -> None:
        """Rain — cloud sprite + vertical drop dots (no diagonal streaks at 8px)."""
        self._plot_icon_pixels(draw, x, y, s, 5, 4, (
            (1, 0), (2, 0), (3, 0),
            (0, 1), (1, 1), (2, 1), (3, 1), (4, 1),
            (1, 2), (2, 2), (3, 2),
            (1, 3), (4, 3),
        ) if drops <= 2 else (
            (1, 0), (2, 0), (3, 0),
            (0, 1), (1, 1), (2, 1), (3, 1), (4, 1),
            (1, 2), (2, 2), (3, 2),
            (0, 3), (2, 3), (4, 3),
        ))

    def _draw_rain_streaks(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        s: int,
        count: int,
        streak_len: int,
    ) -> None:
        top = y + s * 2 // 3
        bottom = min(y + s - 1, top + streak_len)
        step = max(2, (s - s // 4) // max(count, 1))
        start_x = x + s // 4
        for i in range(count):
            sx = start_x + i * step
            draw.line([(sx, top), (sx - max(1, s // 8), bottom)], fill=COLOR_ORANGE, width=1)

    def _icon_drizzle(self, draw: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
        """Drizzle (51–55): cloud + two short streaks."""
        if s <= 9:
            self._icon_rain_small(draw, x, y, s, drops=2)
            return
        self._icon_cloud(draw, x, y, s)
        self._draw_rain_streaks(draw, x, y, s, count=2, streak_len=max(1, s // 5))

    def _icon_rain_light(self, draw: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
        """Light rain (61, 63, 80, 81): cloud + two streaks."""
        if s <= 9:
            self._icon_rain_small(draw, x, y, s, drops=2)
            return
        self._icon_cloud(draw, x, y, s)
        self._draw_rain_streaks(draw, x, y, s, count=2, streak_len=max(2, s // 3))

    def _icon_rain_heavy(self, draw: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
        """Heavy rain (65, 82): cloud + four streaks."""
        if s <= 9:
            self._icon_rain_small(draw, x, y, s, drops=4)
            return
        self._icon_cloud(draw, x, y, s)
        self._draw_rain_streaks(draw, x, y, s, count=4, streak_len=max(2, s // 3))

    def _icon_snow(self, draw: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
        if s <= 9:
            self._plot_icon_pixels(draw, x, y, s, 5, 4, (
                (1, 0), (2, 0), (3, 0),
                (0, 1), (1, 1), (2, 1), (3, 1), (4, 1),
                (1, 2), (2, 2), (3, 2),
                (1, 3), (2, 3), (3, 3),
            ))
            return
        self._icon_cloud(draw, x, y, s)
        cx = x + s // 2
        base = y + s - 2
        draw.point((cx, base), fill=COLOR_ORANGE)
        draw.point((cx - 1, base + 1), fill=COLOR_ORANGE)
        draw.point((cx + 1, base + 1), fill=COLOR_ORANGE)

    def _icon_ice(self, draw: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
        self._icon_rain_light(draw, x, y, s)
        draw.line([(x + 1, y + s - 1), (x + s - 2, y + s - 1)], fill=COLOR_ORANGE, width=1)

    def _icon_fog(self, draw: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
        for i in range(3):
            yy = y + s // 4 + i * max(2, s // 5)
            draw.line([(x + 1, yy), (x + s - 2, yy)], fill=COLOR_ORANGE, width=1)

    def _icon_storm(self, draw: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
        self._icon_cloud(draw, x, y, max(8, s - 1))
        draw.line([(x + s * 2 // 3, y + s // 2), (x + s // 2, y + s - 1)], fill=COLOR_ORANGE, width=1)
        draw.line([(x + s // 2, y + s - 1), (x + s * 3 // 4, y + s * 2 // 3)], fill=COLOR_ORANGE, width=1)

    def _weather_coordinates(self) -> Tuple[float, float]:
        if (
            self.weather_at_stop
            and self._stop_latitude is not None
            and self._stop_longitude is not None
        ):
            return self._stop_latitude, self._stop_longitude
        return self._config_latitude, self._config_longitude

    # --- VVO API ---

    @staticmethod
    def _parse_vvo_coords(lat_str: str, lng_str: str) -> Tuple[Optional[float], Optional[float]]:
        if not lat_str or not lng_str or lat_str == "0" or lng_str == "0":
            return None, None
        try:
            if "." in lat_str or "." in lng_str:
                return float(lat_str), float(lng_str)
            converted = _gk4_to_wgs(int(lat_str), int(lng_str))
            if converted is None:
                return None, None
            return converted
        except (TypeError, ValueError):
            return None, None

    def _parse_point(self, point: Any) -> Tuple[str, str, Optional[float], Optional[float]]:
        if isinstance(point, dict):
            stop_id = str(point.get("Id") or point.get("DataId") or "")
            name = str(point.get("Name") or "")
            lat, lon = self._parse_vvo_coords(
                str(point.get("Latitude") or point.get("Lat") or ""),
                str(point.get("Longitude") or point.get("Lng") or ""),
            )
            return stop_id, name, lat, lon
        parts = str(point).split("|")
        stop_id = parts[0] if parts else ""
        name = parts[3] if len(parts) > 3 else ""
        lat_str = parts[4] if len(parts) > 4 else ""
        lng_str = parts[5] if len(parts) > 5 else ""
        lat, lon = self._parse_vvo_coords(lat_str, lng_str)
        return stop_id, name, lat, lon

    def _lookup_stop(self, query: str) -> Tuple[str, str, Optional[float], Optional[float]]:
        params = urllib.parse.urlencode({"query": query, "stopsOnly": "true", "format": "json"})
        data = self._get_json(f"{VVO_BASE_URL}/tr/pointfinder?{params}")
        if data.get("Status", {}).get("Code") != "Ok":
            raise RuntimeError(data.get("Status", {}).get("Message", "Stop lookup failed"))
        points = data.get("Points") or []
        if not points:
            raise RuntimeError(f"No stops found for: {query}")
        stop_id, name, lat, lon = self._parse_point(points[0])
        if not stop_id:
            raise RuntimeError(f"Could not resolve stop ID for: {query}")
        return stop_id, name, lat, lon

    @staticmethod
    def _normalize_stop_name(name: str) -> str:
        text = name.strip()
        text = re.sub(r"^Dresden\s+", "", text, flags=re.IGNORECASE)
        return text

    def _fetch_trip_stops_ahead(
        self,
        trip_id: str,
        trip_time: Any,
        stop_id: str,
    ) -> List[str]:
        """Stops from the current position onward (VVO dm/trip), for route-based nicknames."""
        if not trip_id or not trip_time or not stop_id:
            return []
        try:
            data = self._post_json(
                f"{VVO_BASE_URL}/dm/trip",
                {
                    "format": "json",
                    "tripid": trip_id,
                    "time": trip_time,
                    "stopid": stop_id,
                },
                timeout=VVO_TRIP_TIMEOUT_S,
            )
            if data.get("Status", {}).get("Code") != "Ok":
                return []
            stops = data.get("Stops") or []
            current_idx = next(
                (idx for idx, stop in enumerate(stops) if stop.get("Position") == "Current"),
                None,
            )
            if current_idx is None:
                return []
            return [
                self._normalize_stop_name(str(stop.get("Name", "")))
                for stop in stops[current_idx:]
                if stop.get("Name")
            ]
        except Exception as exc:
            self.logger.debug("Trip lookup failed for %s: %s", trip_id, exc)
            return []

    def _resolve_vvo_stop(self) -> Tuple[str, Optional[float], Optional[float]]:
        """Cache VVO stop ID so refresh cycles do not call pointfinder every time."""
        query = self.stop_query
        if self._vvo_stop_id and self._vvo_stop_query == query:
            return self._vvo_stop_id, self._stop_latitude, self._stop_longitude
        stop_id, _name, stop_lat, stop_lon = self._lookup_stop(query)
        if not stop_id:
            raise RuntimeError(f"Could not resolve stop ID for: {query}")
        self._vvo_stop_id = stop_id
        self._vvo_stop_query = query
        self._stop_latitude = stop_lat
        self._stop_longitude = stop_lon
        return stop_id, stop_lat, stop_lon

    def _attach_route_stops(self, results: List[Dict[str, Any]], raw_deps: List[Dict[str, Any]], stop_id: str) -> None:
        if not self._destination_aliases or not results:
            return
        by_trip: Dict[str, Dict[str, Any]] = {}
        for dep in raw_deps:
            tid = str(dep.get("Id", ""))
            if tid:
                by_trip[tid] = dep

        def trip_for(row: Dict[str, Any]) -> Tuple[str, List[str]]:
            trip_id = str(row.get("_trip_id", ""))
            dep = by_trip.get(trip_id, {})
            stops = self._fetch_trip_stops_ahead(
                trip_id,
                dep.get("RealTime") or dep.get("ScheduledTime"),
                stop_id,
            )
            return trip_id, stops

        with ThreadPoolExecutor(max_workers=VVO_TRIP_WORKERS) as pool:
            futures = [pool.submit(trip_for, row) for row in results]
            stops_by_trip: Dict[str, List[str]] = {}
            try:
                for fut in as_completed(futures, timeout=VVO_TRIP_TIMEOUT_S * 2):
                    trip_id, stops = fut.result()
                    stops_by_trip[trip_id] = stops
            except Exception as exc:
                self.logger.warning("Route lookup partial failure: %s", exc)

        for row in results:
            trip_id = str(row.pop("_trip_id", ""))
            row["route_stops_ahead"] = stops_by_trip.get(trip_id, [])

    def _fetch_departures(self) -> Tuple[str, List[Dict[str, Any]], Optional[float], Optional[float]]:
        stop_id, stop_lat, stop_lon = self._resolve_vvo_stop()
        fetch_limit = self.max_departures + 8 + len(self._hidden_lines) * 4
        body = {
            "format": "json",
            "stopid": stop_id,
            "isarrival": False,
            "limit": fetch_limit,
            "shorttermchanges": True,
        }
        data = self._post_json(f"{VVO_BASE_URL}/dm", body)
        if data.get("Status", {}).get("Code") != "Ok":
            raise RuntimeError(data.get("Status", {}).get("Message", "VVO API error"))

        station_name = str(data.get("Name") or self.stop_query)
        now = datetime.now(self.timezone)
        results: List[Dict[str, Any]] = []

        for dep in data.get("Departures") or []:
            if self.tram_only and dep.get("Mot") != "Tram":
                continue
            line = str(dep.get("LineName", "?")).strip()
            if line in self._hidden_lines:
                continue
            when = self._parse_vvo_time(dep.get("RealTime") or dep.get("ScheduledTime"))
            if when is None:
                continue
            when_local = when.astimezone(self.timezone)
            results.append({
                "line": line,
                "direction": str(dep.get("Direction", "")),
                "departs_at": when_local.isoformat(),
                "route_stops_ahead": [],
                "_trip_id": str(dep.get("Id", "")),
            })
            if len(results) >= self.max_departures:
                break

        self._attach_route_stops(results, list(data.get("Departures") or []), stop_id)
        return station_name, results, stop_lat, stop_lon

    # --- Open-Meteo API ---

    def _fetch_weather(self, latitude: float, longitude: float) -> Dict[str, Any]:
        tz = urllib.parse.quote(str(self.timezone))
        url = (
            f"{OPEN_METEO_URL}?latitude={latitude}&longitude={longitude}"
            f"&current=temperature_2m,weather_code"
            f"&hourly=temperature_2m,weather_code"
            f"&past_hours=12&forecast_hours=48"
            f"&timezone={tz}"
        )
        data = self._get_json(url)
        current = data.get("current") or {}
        hourly = data.get("hourly") or {}
        return {
            "temp": current.get("temperature_2m"),
            "weather_code": current.get("weather_code"),
            "hourly": {
                "time": hourly.get("time") or [],
                "weather_code": hourly.get("weather_code") or [],
                "temperature_2m": hourly.get("temperature_2m") or [],
            },
        }

    def _next_forecast_targets(self, now: datetime, count: int = FORECAST_SLOT_COUNT) -> List[datetime]:
        """Next `count` anchor times (6h/13h/20h) strictly after `now`, chronological."""
        upcoming: List[datetime] = []
        day = now.date()
        for _ in range(4):
            for hour in FORECAST_ANCHOR_HOURS:
                target = datetime(day.year, day.month, day.day, hour, 0, tzinfo=now.tzinfo)
                if target > now:
                    upcoming.append(target)
            day += timedelta(days=1)
            if len(upcoming) >= count:
                break
        upcoming.sort()
        return upcoming[:count]

    def _parse_hourly_time(self, ts: str) -> datetime:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=self.timezone)
        return dt.astimezone(self.timezone)

    def _hourly_forecast_slots(
        self,
        hourly: Dict[str, Any],
        now: datetime,
    ) -> List[Dict[str, Any]]:
        times = hourly.get("time") or []
        codes = hourly.get("weather_code") or []
        temps = hourly.get("temperature_2m") or []
        slots: List[Dict[str, Any]] = []

        for target in self._next_forecast_targets(now):
            best_idx: Optional[int] = None
            best_delta: Optional[float] = None
            for idx, ts in enumerate(times):
                dt = self._parse_hourly_time(ts)
                delta = abs((dt - target).total_seconds())
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best_idx = idx
            if best_idx is not None and best_idx < len(codes):
                icon = self._code_to_icon(int(codes[best_idx]))
                temp = temps[best_idx] if best_idx < len(temps) else None
            else:
                icon = "cloud"
                temp = None
            slots.append({
                "label": f"{target.hour}h",
                "hour": target.hour,
                "icon": icon,
                "temp": temp,
            })
        return slots

    def _code_to_icon(self, code: int) -> WeatherIcon:
        if code in ICE_CODES:
            return "ice"
        if code in SNOW_CODES:
            return "snow"
        if code in STORM_CODES:
            return "storm"
        if code in DRIZZLE_CODES:
            return "drizzle"
        if code in RAIN_HEAVY_CODES:
            return "rain_heavy"
        if code in RAIN_LIGHT_CODES:
            return "rain_light"
        if code in FOG_CODES:
            return "fog"
        if code in PARTLY_CLOUDY_CODES:
            return "partly_cloudy"
        if code in OVERCAST_CODES:
            return "cloud"
        if code in CLEAR_CODES:
            return "sun"
        return "cloud"

    @staticmethod
    def _parse_vvo_time(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        match = DATE_RE.search(value)
        if not match:
            return None
        return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=ZoneInfo("UTC"))

    @staticmethod
    def _post_json(url: str, body: Dict[str, Any], timeout: float = 15) -> Dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=UTF-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _get_json(url: str, timeout: float = 15) -> Dict[str, Any]:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # --- Destination text helpers ---

    def _dest_name_max_width(self, left_w: int) -> int:
        """Fixed width for destination text — always reserve two-digit minutes column."""
        mins_col_w = self._font_body.text_width("9" * MINS_COLUMN_CHARS)
        return left_w - DEST_NAME_X - mins_col_w - MINS_RIGHT_PAD

    @staticmethod
    def _format_direction_display(text: str) -> str:
        """Render VVO's ``->`` as a single narrow arrow on the matrix."""
        return text.replace("->", DIRECTION_ARROW)

    @staticmethod
    def _normalize_direction(direction: str) -> str:
        """Tighten VVO direction strings for the narrow LED column."""
        text = direction.strip()
        text = re.sub(r"\s*->\s*", "->", text)
        text = re.sub(r"\s+-\s+", "-", text)
        text = re.sub(r"\s+", " ", text)
        # VVO short-turn labels: "Löbtau->Li.12" → "Löbtau->12"
        text = re.sub(r"->Li\.(\d+)\s*$", r"->\1", text, flags=re.IGNORECASE)
        return text

    def _direction_short_turn_suffix(self, text: str) -> Optional[str]:
        """Short-turn tail after ``->`` (e.g. EV); omit plain endhaltestellen names."""
        if "->" not in text:
            return None
        _, _, tail = text.partition("->")
        tail = tail.strip()
        return tail or None

    def _match_route_alias(self, route_stops_ahead: List[str]) -> Optional[str]:
        """Return nickname when a configured stop lies ahead on this trip."""
        for src in sorted(self._destination_aliases, key=len, reverse=True):
            dst = self._destination_aliases[src]
            if not src:
                continue
            src_l = src.lower()
            for stop in route_stops_ahead:
                stop_l = stop.lower()
                if stop_l == src_l or stop_l.startswith(src_l) or src_l in stop_l:
                    return dst
        return None

    def _apply_direction_alias(self, text: str) -> str:
        """Fallback: match alias keys against the VVO Direction / endhaltestelle label."""
        for src in sorted(self._destination_aliases, key=len, reverse=True):
            dst = self._destination_aliases[src]
            if not src:
                continue

            if text == src or text.lower() == src.lower():
                return dst

            if text.lower().startswith(src.lower()):
                rest = text[len(src):]
                if not rest or rest[0] in "->,- ":
                    return dst + rest

            if "->" in text:
                head, _, tail = text.partition("->")
                if head.strip().lower() == src.lower():
                    return dst + ("->" + tail if tail else "")

            pattern = re.compile(r"(?<!\w)" + re.escape(src) + r"(?!\w)", re.IGNORECASE)
            if pattern.search(text):
                return pattern.sub(dst, text, count=1)

        return text

    def _display_direction(
        self,
        direction: str,
        route_stops_ahead: Optional[List[str]] = None,
    ) -> str:
        """Apply nicknames from stops ahead on the route, then fall back to Direction text."""
        text = self._normalize_direction(direction)
        if not self._destination_aliases:
            return self._format_direction_display(text)

        nickname: Optional[str] = None
        if route_stops_ahead:
            nickname = self._match_route_alias(route_stops_ahead)
        if nickname:
            suffix = self._direction_short_turn_suffix(text)
            if suffix:
                return self._format_direction_display(nickname + "->" + suffix)
            return nickname

        return self._format_direction_display(self._apply_direction_alias(text))

    def on_config_change(self, new_config: Dict[str, Any]) -> None:
        """Apply runtime config updates (e.g. after editing config_secrets.json)."""
        self.config = new_config
        self._sync_config()

    def _truncate(self, text: str, max_px: int, font: BdfFont, **font_kw: Any) -> str:
        if font.text_width(text, **font_kw) <= max_px:
            return text
        trimmed = text.rstrip()
        while trimmed and font.text_width(trimmed + ".", **font_kw) > max_px:
            trimmed = trimmed[:-1].rstrip()
        return (trimmed + ".") if trimmed else text[:1]

    # --- Plugin hooks ---

    def validate_config(self) -> bool:
        if not super().validate_config():
            return False
        if not self.stop_query:
            self.logger.error("stop_id is required (stop name or numeric VVO ID)")
            return False
        return True

    def get_display_duration(self) -> float:
        return float(self.config.get("display_duration", 86400))
