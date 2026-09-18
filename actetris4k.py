import array
import math
import random
import sys
import threading
import time

import pygame

# Game Constants
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 600
BLOCK_SIZE = 20
BOARD_WIDTH = 10
BOARD_HEIGHT = 20
FPS = 60

APP_TITLE = "AC'S Tetris 0.1"

# Colors matching Famicom / NES Tetris palette style
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
YELLOW = (252, 224, 0)
GRAY = (120, 120, 120)
BORDER_COLOR = (150, 150, 150)
BG_BLUE = (15, 60, 200)
HEART_RED = (220, 20, 20)

MENU_ITEMS = ("1 PLAYER", "EXIT")

SHAPES = {
    "I": [[1, 1, 1, 1]],
    "J": [[1, 0, 0], [1, 1, 1]],
    "L": [[0, 0, 1], [1, 1, 1]],
    "O": [[1, 1], [1, 1]],
    "S": [[0, 1, 1], [1, 1, 0]],
    "T": [[0, 1, 0], [1, 1, 1]],
    "Z": [[1, 1, 0], [0, 1, 1]],
}

SHAPE_COLORS = {
    "I": (240, 20, 20),
    "J": (20, 240, 20),
    "L": (20, 20, 240),
    "O": (240, 240, 20),
    "S": (240, 20, 240),
    "T": (20, 240, 240),
    "Z": (240, 120, 20),
}

# --- Korobeiniki / Tetris Type A (gameplay only) ---
# Lead rhythm & pitches from published MuseScore-style piano scores
# (mfiles.co.uk Korobeiniki PDF/MIDI + community Arduino/MuseScore lead sheets).
# Durations: 4=quarter→2 eighths, 8=eighth→1, dotted-quarter→3 eighths.
SR = 44100
NES_CPU = 1789773.0
TETRIS_BPM = 150
E8_MS = max(40, int(round(60000.0 / TETRIS_BPM / 2.0)))
DUTY_LEAD = 0.5
DUTY_HARM = 0.25
STAC_LEAD = 0.95
STAC_HARM = 0.88

_NOTE = {
    "R": 0,
    "A2": 45, "E3": 52, "F3": 53, "G3": 55, "A3": 57, "B3": 59,
    "C4": 60, "D4": 62, "E4": 64, "F4": 65, "G4": 67, "A4": 69, "B4": 71,
    "C5": 72, "D5": 74, "E5": 76, "F5": 78, "G5": 79, "A5": 81,
}


def _parse_score(rows: tuple[str, ...]) -> tuple[tuple[int, float], ...]:
    out: list[tuple[int, float]] = []
    for row in rows:
        name, dur = row.split(":")
        out.append((_NOTE.get(name, 0), float(dur)))
    return tuple(out)


# MuseScore / mfiles Korobeiniki piano melody (section A + B, one pass)
_MUSESCORE_KORO = (
    "E5:2", "B4:1", "C5:1",
    "D5:2", "C5:1", "B4:1",
    "A4:2", "A4:1", "C5:1",
    "E5:2", "D5:1", "C5:1",
    "B4:3", "C5:1",
    "D5:2", "E5:2",
    "C5:2", "A4:2",
    "A4:2", "R:2",
    "D5:2", "F5:1",
    "A5:2", "G5:1", "F5:1",
    "E5:2", "E5:1", "C5:1",
    "E5:2", "D5:1", "C5:1",
    "B4:2", "B4:1", "C5:1",
    "D5:2", "E5:2",
    "C5:2", "A4:2",
    "A4:2", "R:2",
)

_TYPE_A_LEAD = _parse_score(_MUSESCORE_KORO)


def _harm_from_lead(lead: tuple[tuple[int, float], ...]) -> tuple[tuple[int, float], ...]:
    """Harmony: minor third below lead (typical Korobeiniki piano accompaniment)."""
    out: list[tuple[int, float]] = []
    for midi, dur in lead:
        if midi <= 0:
            out.append((0, dur))
            continue
        h = midi - 4
        while h < 55:
            h += 12
        while h > 79:
            h -= 12
        out.append((h, dur))
    return tuple(out)


def _bass_from_lead(lead: tuple[tuple[int, float], ...]) -> tuple[tuple[int, float], ...]:
    """Triangle: A2 / E3 pedal, two eighths each."""
    out: list[tuple[int, float]] = []
    toggle = 0
    for midi, dur in lead:
        if midi <= 0:
            out.append((0, dur))
            continue
        root = 45 if toggle == 0 else 52  # A2 / E3
        toggle ^= 1
        out.append((root, dur))
    return tuple(out)


_TYPE_A_HARM = _harm_from_lead(_TYPE_A_LEAD)
_TYPE_A_BASS = _bass_from_lead(_TYPE_A_LEAD)

_PERIOD: dict[int, int] = {}


def _e8_ms(eighths: float) -> int:
    return max(40, int(round(E8_MS * eighths)))


def _hz(midi: int) -> float:
    if midi <= 0:
        return 0.0
    if midi not in _PERIOD:
        f = 440.0 * (2.0 ** ((midi - 69) / 12.0))
        _PERIOD[midi] = max(0, int(round(NES_CPU / (16.0 * f) - 1.0)))
    return NES_CPU / (16.0 * (_PERIOD[midi] + 1))


def _make_tone(
    freq: float,
    ms: int,
    vol: float,
    *,
    duty: float = DUTY_LEAD,
    triangle: bool = False,
    noise: bool = False,
) -> pygame.mixer.Sound | None:
    if ms < 1 or vol <= 0:
        return None
    if not noise and freq <= 0:
        return None
    n = max(1, int(SR * ms / 1000))
    amp = int(26000 * min(1.0, vol))
    buf = array.array("h", [0] * n)
    inc = freq / SR if freq > 0 and not noise else 0.0
    ph = 0.0
    for i in range(n):
        t = i / max(1, n)
        if t < 0.03:
            env = t / 0.03
        elif t > 0.86:
            env = max(0.0, (1.0 - t) / 0.14)
        else:
            env = 1.0
        if noise:
            s = int(amp * 0.5 * env * (random.random() * 2.0 - 1.0))
        else:
            ph += inc
            if ph >= 1.0:
                ph -= 1.0
            if triangle:
                tri = 2.0 * abs(2.0 * (ph - math.floor(ph + 0.5)) - 1.0) - 1.0
                s = int(amp * tri * env)
            else:
                s = int(amp * env) if ph < duty else int(-amp * env)
        buf[i] = s
    return pygame.mixer.Sound(buffer=buf)


def _play_tone(
    midi: int,
    ms: int,
    vol: float,
    *,
    duty: float = DUTY_LEAD,
    triangle: bool = False,
    stac: float = STAC_LEAD,
) -> None:
    play_ms = max(24, int(ms * stac)) if not triangle else ms
    snd = _make_tone(_hz(midi), play_ms, vol, duty=duty, triangle=triangle)
    if snd:
        snd.play()


def _play_noise(ms: int, vol: float) -> None:
    snd = _make_tone(0, max(18, int(ms * 0.35)), vol, noise=True)
    if snd:
        snd.play()


def _zip_type_a(
    lead: tuple[tuple[int, float], ...],
    harm: tuple[tuple[int, float], ...],
    bass: tuple[tuple[int, float], ...],
) -> tuple[tuple[int, int, int, int, bool], ...]:
    n = len(lead)
    out: list[tuple[int, int, int, int, bool]] = []
    for i in range(n):
        m, me = lead[i]
        h, _ = harm[i] if i < len(harm) else (0, 1.0)
        b, _ = bass[i] if i < len(bass) else (0, 1.0)
        fr = _e8_ms(me)
        nz = m > 0 and i % 4 == 2
        out.append((m, h, b, fr, nz))
    return tuple(out)


TYPE_A_TRACK = _zip_type_a(_TYPE_A_LEAD, _TYPE_A_HARM, _TYPE_A_BASS)


class TetrisMusic:
    """Loops Korobeiniki (Type A) during play; silent on main menu."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.enabled = True
        self.lead_vol = 0.42
        self.harm_vol = 0.16
        self.bass_vol = 0.26
        self.noise_vol = 0.07

    def start(self) -> None:
        if not pygame.mixer.get_init():
            return
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None

    def _play_track(self, track: tuple[tuple[int, int, int, int, bool], ...]) -> None:
        for mel, harm, bass, fr, nz in track:
            if self._stop.is_set():
                return
            if self.enabled:
                if mel > 0:
                    _play_tone(
                        mel, fr, self.lead_vol, duty=DUTY_LEAD, stac=STAC_LEAD
                    )
                if harm > 0 and mel > 0:
                    _play_tone(
                        harm,
                        fr,
                        self.harm_vol,
                        duty=DUTY_HARM,
                        stac=STAC_HARM,
                    )
                if bass > 0:
                    _play_tone(bass, fr, self.bass_vol, triangle=True, stac=1.0)
                if nz:
                    _play_noise(fr, self.noise_vol)
            time.sleep(fr / 1000.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._play_track(TYPE_A_TRACK)


_game_music: TetrisMusic | None = None


def _get_music() -> TetrisMusic:
    global _game_music
    if _game_music is None:
        _game_music = TetrisMusic()
    return _game_music


def _draw_block_letter(screen, x, y, letter, color, size=10):
    """Tiny 5x5 block font for NES-style logo accents."""
    glyphs = {
        "T": ["11111", "00100", "00100", "00100", "00100"],
        "E": ["11111", "10000", "11110", "10000", "11111"],
        "R": ["11110", "10001", "11110", "10100", "10001"],
        "I": ["11111", "00100", "00100", "00100", "11111"],
        "S": ["01111", "10000", "01110", "00001", "11110"],
        "A": ["01110", "10001", "11111", "10001", "10001"],
        "C": ["01111", "10000", "10000", "10000", "01111"],
        "'": ["010", "100", "000", "000", "000"],
    }
    pat = glyphs.get(letter.upper())
    if not pat:
        return
    for r, row in enumerate(pat):
        for c, ch in enumerate(row):
            if ch == "1":
                pygame.draw.rect(
                    screen, color, (x + c * size, y + r * size, size - 1, size - 1)
                )


def run_menu(screen, clock):
    """NES-style title menu centered on a black screen."""
    pygame.key.set_repeat(0, 0)
    title_font = pygame.font.SysFont("Courier", 52, bold=True)
    sub_font = pygame.font.SysFont("Courier", 22, bold=True)
    menu_font = pygame.font.SysFont("Courier", 32, bold=True)
    hint_font = pygame.font.SysFont("Courier", 20)

    selected = 0
    blink_on = True
    blink_ms = 0

    cx = SCREEN_WIDTH // 2
    cy = SCREEN_HEIGHT // 2

    while True:
        dt = clock.tick(FPS)
        blink_ms += dt
        if blink_ms >= 450:
            blink_ms = 0
            blink_on = not blink_on

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return "quit"
            if event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE,):
                    return "quit"
                if event.key in (pygame.K_UP, pygame.K_w):
                    selected = (selected - 1) % len(MENU_ITEMS)
                elif event.key in (pygame.K_DOWN, pygame.K_s):
                    selected = (selected + 1) % len(MENU_ITEMS)
                elif event.key in (pygame.K_RETURN, pygame.K_SPACE):
                    return "play" if selected == 0 else "quit"

        screen.fill(BLACK)

        # FIX: Replaced SHAPE_COLORS["R"] (which crashed) with valid dictionary values
        logo_y = cy - 155
        colors = (
            SHAPE_COLORS["I"],
            SHAPE_COLORS["O"],
            SHAPE_COLORS["T"],
            SHAPE_COLORS["J"], # Used J color instead of non-existent R
            SHAPE_COLORS["I"],
            SHAPE_COLORS["S"],
        )
        letters = "TETRIS"
        total_w = len(letters) * 6 * 10
        lx = cx - total_w // 2
        for i, ch in enumerate(letters):
            _draw_block_letter(screen, lx + i * 60, logo_y, ch, colors[i], 10)

        # Main title — centered
        title = title_font.render("AC'S Tetris 0.1", True, WHITE)
        screen.blit(title, title.get_rect(center=(cx, cy - 35)))

        sub = sub_font.render("(C) AC HOLDINGS", True, GRAY)
        screen.blit(sub, sub.get_rect(center=(cx, cy + 5)))

        # Menu block (NES type-A / type-B layout, centered)
        menu_top = cy + 55
        for i, label in enumerate(MENU_ITEMS):
            is_sel = i == selected
            color = YELLOW if is_sel else WHITE
            cursor = "► " if is_sel else "   "
            text = menu_font.render(cursor + label, True, color)
            screen.blit(text, text.get_rect(center=(cx, menu_top + i * 44)))

        if blink_on:
            hint = hint_font.render("PRESS ENTER", True, WHITE)
            screen.blit(hint, hint.get_rect(center=(cx, SCREEN_HEIGHT - 48)))

        nav = hint_font.render("↑↓ SELECT   ENTER START", True, GRAY)
        screen.blit(nav, nav.get_rect(center=(cx, SCREEN_HEIGHT - 22)))

        pygame.display.flip()


def spawn_x(shape_type: str) -> int:
    """Center piece in the 10-wide well (fixes I/O misalignment at spawn)."""
    w = max(len(row) for row in SHAPES[shape_type])
    return (BOARD_WIDTH - w) // 2


class Tetramino:
    def __init__(self, x, y, shape_type):
        self.x = x
        self.y = y
        self.type = shape_type
        self.shape = [row[:] for row in SHAPES[shape_type]]
        self.color = SHAPE_COLORS[shape_type]

    def rotate(self):
        self.shape = [list(col) for col in zip(*self.shape[::-1])]

    def rotated_shape(self):
        return [list(col) for col in zip(*self.shape[::-1])]


class TetrisFamicom:
    def __init__(self, screen, clock):
        self.screen = screen
        self.clock = clock

        self.board = [[None for _ in range(BOARD_WIDTH)] for _ in range(BOARD_HEIGHT)]

        self.score = 0
        self.lives = 3
        self.round = 5
        self.stage = 0
        self.lines_target = 25

        self.bag: list[str] = []
        self.game_over = False
        self.won = False

        self.fall_time = 0
        self.fall_speed = 500

        self.next_piece = self.new_piece()
        self.current_piece = self.new_piece()
        if self.check_collision(self.current_piece):
            self.handle_life_loss()

    def _pull_shape(self) -> str:
        if not self.bag:
            self.bag = list(SHAPES.keys())
            random.shuffle(self.bag)
        return self.bag.pop()

    # FIX: Cleaned type hinting syntax to support native implementations seamlessly
    def new_piece(self, shape_type: str = None) -> Tetramino:
        st = shape_type or self._pull_shape()
        return Tetramino(spawn_x(st), 0, st)

    def check_collision(self, piece, offset_x=0, offset_y=0, shape=None):
        shape = shape or piece.shape
        for r, row in enumerate(shape):
            for c, val in enumerate(row):
                if not val:
                    continue
                new_x = piece.x + c + offset_x
                new_y = piece.y + r + offset_y
                if new_x < 0 or new_x >= BOARD_WIDTH or new_y >= BOARD_HEIGHT:
                    return True
                if new_y >= 0 and self.board[new_y][new_x] is not None:
                    return True
        return False

    def lock_piece(self):
        if self.game_over:
            return

        for r, row in enumerate(self.current_piece.shape):
            for c, val in enumerate(row):
                if not val:
                    continue
                by = self.current_piece.y + r
                bx = self.current_piece.x + c
                if 0 <= by < BOARD_HEIGHT and 0 <= bx < BOARD_WIDTH:
                    self.board[by][bx] = self.current_piece.color

        self.clear_lines()
        if self.game_over:
            return

        self.current_piece = self.next_piece
        self.next_piece = self.new_piece()
        self.fall_time = 0
        if self.check_collision(self.current_piece):
            self.handle_life_loss()

    def handle_life_loss(self):
        if self.game_over:
            return
        self.lives -= 1
        self.board = [[None for _ in range(BOARD_WIDTH)] for _ in range(BOARD_HEIGHT)]
        if self.lives <= 0:
            self.game_over = True
            return
        self.bag = []
        self.current_piece = self.new_piece()
        self.next_piece = self.new_piece()
        self.fall_time = 0
        if self.check_collision(self.current_piece):
            self.game_over = True

    def clear_lines(self):
        cleared = 0
        r = BOARD_HEIGHT - 1
        while r >= 0:
            if all(cell is not None for cell in self.board[r]):
                del self.board[r]
                self.board.insert(0, [None for _ in range(BOARD_WIDTH)])
                cleared += 1
            else:
                r -= 1

        if cleared <= 0:
            return

        self.score += (cleared**2) * 100 * self.round
        self.lines_target -= cleared

        while self.lines_target <= 0:
            self.stage += 1
            self.lines_target += 25
            self.fall_speed = max(80, self.fall_speed - 40)
            if self.stage > 9:
                self.won = True
                self.game_over = True
                return

    def move(self, dx):
        if self.game_over:
            return
        if not self.check_collision(self.current_piece, offset_x=dx):
            self.current_piece.x += dx

    def try_move_down(self) -> bool:
        """Move down one row; return True if moved."""
        if self.game_over:
            return False
        if not self.check_collision(self.current_piece, offset_y=1):
            self.current_piece.y += 1
            return True
        return False

    def soft_drop(self):
        if self.try_move_down():
            self.score += 1
        else:
            self.lock_piece()

    def gravity_step(self):
        if not self.try_move_down():
            self.lock_piece()

    def hard_drop(self):
        if self.game_over:
            return
        while self.try_move_down():
            self.score += 2
        self.lock_piece()

    def rotate_piece(self):
        if self.game_over:
            return
        rotated = self.current_piece.rotated_shape()
        for kick in (0, -1, 1, -2, 2):
            if not self.check_collision(
                self.current_piece, offset_x=kick, shape=rotated
            ):
                self.current_piece.shape = [row[:] for row in rotated]
                if kick:
                    self.current_piece.x += kick
                return

    def draw_hud(self):
        font = pygame.font.SysFont("Courier", 28, bold=True)

        labels = [
            f"SCORE  {self.score:06d}",
            f"ROUND  {self.round}",
            f"STAGE  {self.stage}",
            f"LINES  {self.lines_target:02d}",
        ]

        for i, label in enumerate(labels):
            text_surface = font.render(label, True, WHITE)
            self.screen.blit(text_surface, (50, 150 + i * 50))

        lives_label = font.render("LIVES ", True, WHITE)
        self.screen.blit(lives_label, (50, 100))
        for i in range(self.lives):
            pygame.draw.circle(self.screen, HEART_RED, (160 + i * 25, 115), 8)

        hint = pygame.font.SysFont("Courier", 18).render("ESC  MENU", True, GRAY)
        self.screen.blit(hint, (50, 55))

    def draw_piece(self, piece, offset_x, offset_y):
        for r, row in enumerate(piece.shape):
            for c, val in enumerate(row):
                if not val:
                    continue
                px = piece.x + c
                py = piece.y + r
                if py < 0 or px < 0 or px >= BOARD_WIDTH:
                    continue
                pygame.draw.rect(
                    self.screen,
                    piece.color,
                    (
                        offset_x + px * BLOCK_SIZE,
                        offset_y + py * BLOCK_SIZE,
                        BLOCK_SIZE - 1,
                        BLOCK_SIZE - 1,
                    ),
                )

    def draw_board(self):
        board_offset_x = 300
        board_offset_y = 100

        pygame.draw.rect(
            self.screen,
            BORDER_COLOR,
            (
                board_offset_x - 4,
                board_offset_y - 4,
                BOARD_WIDTH * BLOCK_SIZE + 8,
                BOARD_HEIGHT * BLOCK_SIZE + 8,
            ),
            4,
        )
        pygame.draw.rect(
            self.screen,
            BLACK,
            (
                board_offset_x,
                board_offset_y,
                BOARD_WIDTH * BLOCK_SIZE,
                BOARD_HEIGHT * BLOCK_SIZE,
            ),
        )

        for r, row in enumerate(self.board):
            for c, color in enumerate(row):
                if color:
                    pygame.draw.rect(
                        self.screen,
                        color,
                        (
                            board_offset_x + c * BLOCK_SIZE,
                            board_offset_y + r * BLOCK_SIZE,
                            BLOCK_SIZE - 1,
                            BLOCK_SIZE - 1,
                        ),
                    )

        if not self.game_over:
            self.draw_piece(self.current_piece, board_offset_x, board_offset_y)

        next_box_x = 550
        next_box_y = 250
        font = pygame.font.SysFont("Courier", 24, bold=True)
        next_surface = font.render("NEXT", True, WHITE)
        self.screen.blit(next_surface, (next_box_x, next_box_y - 35))
        pygame.draw.rect(
            self.screen,
            BORDER_COLOR,
            (next_box_x - 4, next_box_y - 4, 5 * BLOCK_SIZE + 8, 4 * BLOCK_SIZE + 8),
            4,
        )
        pygame.draw.rect(
            self.screen,
            BLACK,
            (next_box_x, next_box_y, 5 * BLOCK_SIZE, 4 * BLOCK_SIZE),
        )

        pw = max(len(row) for row in self.next_piece.shape)
        ph = len(self.next_piece.shape)
        ox = (5 - pw) // 2
        oy = (4 - ph) // 2
        for r, row in enumerate(self.next_piece.shape):
            for c, val in enumerate(row):
                if val:
                    pygame.draw.rect(
                        self.screen,
                        self.next_piece.color,
                        (
                            next_box_x + (ox + c) * BLOCK_SIZE,
                            next_box_y + (oy + r) * BLOCK_SIZE,
                            BLOCK_SIZE - 1,
                            BLOCK_SIZE - 1,
                        ),
                    )

    def draw_game_over(self):
        font = pygame.font.SysFont("Courier", 36, bold=True)
        headline = "YOU WIN!" if self.won else "GAME OVER"
        msg = font.render(headline, True, WHITE)
        score = pygame.font.SysFont("Courier", 28, bold=True).render(
            f"SCORE {self.score:06d}", True, WHITE
        )
        hint = pygame.font.SysFont("Courier", 22).render(
            "ESC menu   ENTER back", True, WHITE
        )
        self.screen.blit(msg, msg.get_rect(center=(SCREEN_WIDTH // 2, 260)))
        self.screen.blit(score, score.get_rect(center=(SCREEN_WIDTH // 2, 310)))
        self.screen.blit(hint, hint.get_rect(center=(SCREEN_WIDTH // 2, 360)))

    def run(self):
        pygame.key.set_repeat(140, 40)
        pygame.event.clear()
        music = _get_music()
        music.start()
        try:
            return self._run_loop()
        finally:
            music.stop()

    def _run_loop(self):
        esc_down = False
        while True:
            dt = self.clock.tick(FPS)
            self.screen.fill(BG_BLUE)

            if pygame.key.get_pressed()[pygame.K_ESCAPE] and not esc_down:
                return "menu"
            esc_down = pygame.key.get_pressed()[pygame.K_ESCAPE]

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return "quit"
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        return "menu"
                    if self.game_over:
                        if event.key in (pygame.K_RETURN, pygame.K_SPACE):
                            return "menu"
                        continue
                    if event.key == pygame.K_LEFT:
                        self.move(-1)
                    elif event.key == pygame.K_RIGHT:
                        self.move(1)
                    elif event.key == pygame.K_DOWN:
                        self.soft_drop()
                    elif event.key == pygame.K_UP:
                        self.rotate_piece()
                    elif event.key == pygame.K_SPACE:
                        self.hard_drop()

            if not self.game_over:
                self.fall_time += dt
                if self.fall_time >= self.fall_speed:
                    self.gravity_step()
                    self.fall_time = 0

            self.draw_hud()
            self.draw_board()
            if self.game_over:
                self.draw_game_over()
            pygame.display.flip()


def main():
    pygame.mixer.pre_init(SR, -16, 1, 512)
    pygame.init()
    try:
        pygame.mixer.init()
    except pygame.error:
        pass
    screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
    pygame.display.set_caption(APP_TITLE)
    clock = pygame.time.Clock()
    try:
        while True:
            if run_menu(screen, clock) != "play":
                break
            pygame.event.clear()
            result = TetrisFamicom(screen, clock).run()
            pygame.key.set_repeat(0, 0)
            pygame.event.clear()
            if result == "quit":
                break
            # "menu" or anything else → back to title screen
            continue
    finally:
        _get_music().stop()
        pygame.quit()


if __name__ == "__main__":
    main()
    sys.exit(0)