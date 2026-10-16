"""Full-screen clinical viewer; independent refresh, never an acquisition owner."""
from collections import deque
import multiprocessing as mp
import os
import signal
import time


BACKGROUND = (0, 0, 0)
PANEL = (5, 10, 12)
DIVIDER = (37, 53, 57)
PRIMARY = (232, 242, 242)
SECONDARY = (139, 158, 161)
ACCENT = (44, 225, 193)
LIVE = (92, 229, 113)
BUTTON_BACKGROUND = (10, 30, 29)


def _screen_refresh(pygame):
    import ctypes as ct

    class Mode(ct.Structure):
        _fields_ = [('format', ct.c_uint32), ('w', ct.c_int), ('h', ct.c_int),
                    ('refresh_rate', ct.c_int), ('driverdata', ct.c_void_p)]

    # Use the same SDL library as pygame, not another installed SDL instance.
    sdl = ct.CDLL(pygame.base.__file__)
    query = sdl.SDL_GetCurrentDisplayMode
    query.argtypes, query.restype = [ct.c_int, ct.POINTER(Mode)], ct.c_int
    mode = Mode()
    return mode.refresh_rate if query(0, ct.byref(mode)) == 0 else 0


def _layout(window_size, shape, comparison=False):
    """Return a resolution-independent clinical layout in screen pixels."""
    width, height = window_size
    margin = max(12, round(height * .018))
    header_h = max(64, round(height * .075))
    footer_h = max(28, round(height * .035))
    rail_w = max(270, min(round(width * .19), 390))
    depth_gutter = max(58, round(width * .037))
    range_gutter = max(72, round(width * .045))
    available_w = width - rail_w - depth_gutter - range_gutter - 3 * margin
    available_h = height - header_h - footer_h - 2 * margin
    comparison_gap = max(8, margin // 2) if comparison else 0
    columns = 2 if comparison else 1
    image_scale = min((available_w - comparison_gap) / (columns * shape[1]),
                      available_h / shape[0])
    image_w = max(1, round(columns * shape[1] * image_scale) + comparison_gap)
    image_h = max(1, round(shape[0] * image_scale))
    image_x = margin + depth_gutter + max(0, (available_w - image_w) // 2)
    image_y = header_h + margin + max(0, (available_h - image_h) // 2)
    rail_x = width - rail_w
    return {
        'image': (image_x, image_y, image_w, image_h),
        'rail': (rail_x, header_h, rail_w, height - header_h),
        'header_h': header_h,
        'footer_h': footer_h,
        'margin': margin,
        'range_gutter': range_gutter,
        'comparison_gap': comparison_gap,
    }


def _image_rects(layout, comparison=False):
    image_x, image_y, image_w, image_h = layout['image']
    if not comparison:
        return (layout['image'],)
    gap = layout['comparison_gap']
    left_w = (image_w - gap) // 2
    right_w = image_w - gap - left_w
    return ((image_x, image_y, left_w, image_h),
            (image_x + left_w + gap, image_y, right_w, image_h))


def _font(pygame, size, *, bold=False):
    return pygame.font.SysFont('DejaVu Sans', max(12, round(size)), bold=bold)


def _exit_button_rect(window_size, layout):
    width, _ = window_size
    height = max(34, round(layout['header_h'] * .56))
    button_width = max(82, round(height * 2.2))
    x = width - layout['margin'] - button_width
    y = (layout['header_h'] - height) // 2
    return x, y, button_width, height


def _mimicknet_button_rect(window_size, layout):
    _, height = window_size
    rail_x, _, rail_w, _ = layout['rail']
    margin = layout['margin']
    button_height = max(38, round(layout['header_h'] * .60))
    return (rail_x + margin, height - margin - button_height,
            rail_w - 2 * margin, button_height)


def _label(surface, font, text, position, color=PRIMARY, anchor='topleft'):
    rendered = font.render(str(text), True, color)
    rect = rendered.get_rect()
    setattr(rect, anchor, position)
    surface.blit(rendered, rect)
    return rect


def _format_number(value, digits=1):
    return '--' if value is None else f'{value:.{digits}f}'


def _draw_dynamic_range(surface, pygame, layout, font, dynamic_range_db):
    if dynamic_range_db is None or dynamic_range_db <= 0:
        return
    image_x, image_y, image_w, image_h = layout['image']
    bar_w = max(12, round(surface.get_height() * .012))
    bar_h = image_h
    bar_x = image_x + image_w + max(18, layout['margin'])
    bar_y = image_y
    gradient = pygame.Surface((bar_w, bar_h))
    for offset in range(bar_h):
        gray = round(255 * (1 - offset / (bar_h - 1)))
        pygame.draw.line(gradient, (gray, gray, gray), (0, offset), (bar_w, offset))
    surface.blit(gradient, (bar_x, bar_y))
    pygame.draw.rect(surface, DIVIDER, (bar_x, bar_y, bar_w, bar_h), 1)
    _label(surface, font, f'DR {dynamic_range_db:g} dB',
           (bar_x, bar_y - 7), SECONDARY, 'bottomleft')
    for index in range(6):
        fraction = index / 5
        tick_y = bar_y + round((bar_h - 1) * fraction)
        pygame.draw.line(surface, PRIMARY, (bar_x + bar_w, tick_y),
                         (bar_x + bar_w + 6, tick_y), 1)
        level = 0 if index == 0 else -dynamic_range_db * fraction
        _label(surface, font, f'{level:g}', (bar_x + bar_w + 10, tick_y),
               SECONDARY, 'midleft')


def _chrome(pygame, window_size, shape, layout, scanner, fps, screen_hz,
            mimicknet_available=False, mimicknet_enabled=False):
    """Build the transparent labels/scale layer, refreshed at a low rate."""
    surface = pygame.Surface(window_size, pygame.SRCALPHA)
    width, height = window_size
    image_x, image_y, image_w, image_h = layout['image']
    rail_x, rail_y, rail_w, rail_h = layout['rail']
    header_h, margin = layout['header_h'], layout['margin']

    pygame.draw.rect(surface, (*PANEL, 255), (0, 0, width, header_h))
    pygame.draw.rect(surface, (*PANEL, 255), layout['rail'])
    pygame.draw.line(surface, DIVIDER, (0, header_h - 1), (width, header_h - 1), 1)
    pygame.draw.line(surface, DIVIDER, (rail_x, rail_y), (rail_x, height), 1)
    comparison = mimicknet_available and mimicknet_enabled
    image_rects = _image_rects(layout, comparison)
    for image_rect in image_rects:
        pygame.draw.rect(surface, DIVIDER, image_rect, 1)

    title = _font(pygame, header_h * .36, bold=True)
    header = _font(pygame, header_h * .25, bold=True)
    small = _font(pygame, header_h * .20)
    section = _font(pygame, height * .018, bold=True)
    value = _font(pygame, height * .026, bold=True)
    detail = _font(pygame, height * .018)
    tick = _font(pygame, height * .015)
    dynamic_range_db = scanner.get('dynamic_range_db')

    _label(surface, title, 'SOFTWARE DEFINED ULTRASOUND',
           (margin, header_h // 2), ACCENT, 'midleft')
    _label(surface, header, scanner.get('mode', 'B-MODE').upper(),
           (rail_x - margin, header_h // 2), PRIMARY, 'midright')
    live_x = rail_x + margin
    pygame.draw.circle(surface, LIVE, (live_x + 5, header_h // 2), 5)
    _label(surface, small, 'LIVE', (live_x + 18, header_h // 2), LIVE, 'midleft')
    exit_rect = pygame.Rect(_exit_button_rect(window_size, layout))
    pygame.draw.rect(surface, BUTTON_BACKGROUND, exit_rect, border_radius=5)
    pygame.draw.rect(surface, ACCENT, exit_rect, 2, border_radius=5)
    _label(surface, small, 'EXIT', exit_rect.center, ACCENT, 'center')
    mimicknet_rect = pygame.Rect(_mimicknet_button_rect(window_size, layout))
    if mimicknet_available:
        state = 'ON' if mimicknet_enabled else 'OFF'
        button_color = ACCENT if mimicknet_enabled else SECONDARY
        button_background = BUTTON_BACKGROUND if mimicknet_enabled else PANEL
    else:
        state = 'N/A'
        button_color, button_background = DIVIDER, PANEL
    pygame.draw.rect(surface, button_background, mimicknet_rect, border_radius=5)
    pygame.draw.rect(surface, button_color, mimicknet_rect, 2, border_radius=5)
    _label(surface, small, f'MIMICKNET  {state}', mimicknet_rect.center,
           button_color, 'center')
    if comparison:
        _label(surface, small, 'B-MODE',
               (image_rects[0][0], image_rects[0][1] - 7), SECONDARY, 'bottomleft')
        _label(surface, small, 'MIMICKNET',
               (image_rects[1][0], image_rects[1][1] - 7), ACCENT, 'bottomleft')


    depth_mm = float(scanner.get('depth_mm', 0.))
    major_mm = 10. if depth_mm >= 30. else 5.
    if depth_mm > 0:
        steps = int(depth_mm // major_mm)
        for index in range(steps + 1):
            millimetres = index * major_mm
            y = image_y + round(image_h * millimetres / depth_mm)
            pygame.draw.line(surface, PRIMARY, (image_x - 10, y), (image_x, y), 2)
            _label(surface, tick, f'{millimetres / 10:g}', (image_x - 16, y),
                   SECONDARY, 'midright')
            if index < steps:
                minor_y = image_y + round(
                    image_h * (millimetres + major_mm / 2) / depth_mm)
                pygame.draw.line(surface, SECONDARY, (image_x - 6, minor_y),
                                 (image_x, minor_y), 1)
        _label(surface, tick, 'cm', (image_x - 16, image_y - 7),
               SECONDARY, 'bottomright')

    _draw_dynamic_range(surface, pygame, layout, tick, dynamic_range_db)

    column_x = rail_x + margin
    column_right = width - margin
    y = rail_y + margin
    section_gap = max(18, round(height * .027))
    row_gap = max(8, round(height * .010))

    def heading(text):
        nonlocal y
        _label(surface, section, text.upper(), (column_x, y), ACCENT)
        y += section.get_height() + row_gap

    def row(name, displayed, *, prominent=False, color=PRIMARY):
        nonlocal y
        label_font = detail if not prominent else section
        value_font = value if prominent else detail
        _label(surface, label_font, name, (column_x, y), SECONDARY)
        _label(surface, value_font, displayed, (column_right, y), color, 'topright')
        y += max(label_font.get_height(), value_font.get_height()) + row_gap

    heading('Transducer')
    row('Probe', scanner.get('probe_name', 'L7-3'), prominent=True)
    row('Array', scanner.get('probe_type', 'Linear 128'))
    y += section_gap

    heading('Imaging')
    row('TX Frequency', f"{_format_number(scanner.get('tx_frequency_mhz'))} MHz",
        prominent=True)
    row('Depth', f"{_format_number(depth_mm / 10.)} cm", prominent=True)
    row('Pulse', f"{scanner.get('pulse_cycles', '--')} cycles")
    row('Angles', scanner.get('angles', '--'))
    row('Dynamic Range', f"{dynamic_range_db if dynamic_range_db is not None else '--'} dB")
    y += section_gap

    heading('Acquisition')
    row('Frame Rate', f'{_format_number(fps)} fps', prominent=True, color=LIVE)
    row('Display', f'{screen_hz or "--"} Hz')
    y += section_gap

    heading('Receive')
    if scanner.get('dtgc_start_db') is None:
        row('Attenuation', f"{_format_number(scanner.get('receive_attenuation_db'), 2)} dB")
    else:
        row('TGC', f"{_format_number(scanner.get('dtgc_start_db'))} dB start")
        row('Ramp', f"{_format_number(scanner.get('dtgc_ramp_us'), 0)} us")
    row('Analog LPF', f"{scanner.get('analog_lpf_mhz', '--')} MHz")
    impedance = scanner.get('receive_source_ohms')
    row('Source', 'Default' if impedance is None else f'{impedance} ohm')

    range_label = ('--' if dynamic_range_db is None else f'{dynamic_range_db:g}')
    view_label = 'B-mode + MimickNet' if comparison else 'PW4cm'
    _label(surface, tick, f'{view_label}  •  {range_label} dB grayscale',
           (image_x, height - layout['footer_h'] / 2), SECONDARY, 'midleft')
    return surface


def _viewer(shape, scale, pixels, metadata, lock, stop, closed, channel, scanner,
            pretty, mimicknet_available, mimicknet_enabled):
    # The acquisition owner handles Ctrl+C and sets stop after hardware cleanup.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    os.environ.setdefault('SDL_RENDER_SCALE_QUALITY', 'linear')
    import numpy as np
    import pygame
    from pygame._sdl2.video import Window, Renderer, Texture

    presents, images, skipped = 0, 0, 0
    first_present = last_present = first_image = last_image = None
    sequence = 0
    mimicknet_toggles = 0
    close_reason = None
    report = {}
    try:
        pygame.display.init()
        pygame.font.init()
        refresh = _screen_refresh(pygame)
        fullscreen = False
        if pretty:
            desktop = pygame.display.get_desktop_sizes()[0]
            fullscreen = True
            try:
                window = Window('Software Defined Ultrasound', size=desktop,
                                fullscreen_desktop=True)
            except (TypeError, pygame.error):
                # Keep a usable viewer on window systems without desktop fullscreen.
                fullscreen = False
                window = Window('Software Defined Ultrasound', size=desktop,
                                resizable=True)
        else:
            suffix = 'MimickNet' if mimicknet_enabled else '50 dB'
            window = Window(f'Software Defined Ultrasound · PW4cm · {suffix}',
                            size=(round(shape[1] * scale), round(shape[0] * scale)))
        renderer = Renderer(window, accelerated=True, vsync=True)
        if pretty:
            pygame.mouse.set_visible(True)
            layout = _layout(window.size, shape, mimicknet_enabled)
        else:
            layout = {'image': (0, 0, *window.size)}
            compact_font = pygame.font.Font(None, 28)
        comparison_available = pretty and mimicknet_available
        textures = [None, None] if comparison_available else [None]
        chrome = None
        chrome_at = 0.
        recent = deque(maxlen=120)
        smoothed_fps = None
        shared_shape = (2, *shape) if comparison_available else shape
        shared = np.frombuffer(pixels, np.uint8).reshape(shared_shape)
        channel.send(dict(ready=True, screen_hz=refresh, vsync_requested=True,
                          pretty=pretty, fullscreen=fullscreen, window_size=window.size,
                          image_size=layout['image'][2:],
                          mimicknet_available=mimicknet_available,
                          mimicknet_enabled=mimicknet_enabled,
                          comparison=mimicknet_enabled and comparison_available))
        while True:
            should_close = False
            for event in pygame.event.get():
                if event.type in (pygame.QUIT, pygame.WINDOWCLOSE):
                    close_reason = 'window'
                    should_close = True
                elif (event.type == pygame.KEYDOWN
                      and event.key in (pygame.K_ESCAPE, pygame.K_q)):
                    close_reason = 'keyboard'
                    should_close = True
                elif (pretty and event.type == pygame.MOUSEBUTTONDOWN
                      and event.button == 1
                      and pygame.Rect(_exit_button_rect(window.size, layout)).collidepoint(
                          event.pos)):
                    close_reason = 'button'
                    should_close = True
                elif (pretty and mimicknet_available
                      and event.type == pygame.MOUSEBUTTONDOWN
                      and event.button == 1
                      and pygame.Rect(_mimicknet_button_rect(
                          window.size, layout)).collidepoint(event.pos)):
                    mimicknet_enabled = not mimicknet_enabled
                    mimicknet_toggles += 1
                    channel.send(dict(mimicknet_enabled=mimicknet_enabled,
                                      mimicknet_toggles=mimicknet_toggles,
                                      comparison=mimicknet_enabled))
                    layout = _layout(window.size, shape, mimicknet_enabled)
                    chrome = None
            if should_close:
                break
            with lock:
                incoming, pipeline_fps = int(metadata[0]), metadata[1]
                gray = shared.copy() if incoming != sequence else None
            if gray is not None:
                skipped += max(0, incoming - sequence - 1)
                sequence = incoming
                if pipeline_fps > 0:
                    smoothed_fps = (pipeline_fps if smoothed_fps is None
                                    else .15 * pipeline_fps + .85 * smoothed_fps)
                planes = gray if comparison_available else gray[None, :, :]
                for index, plane in enumerate(planes):
                    rgb = np.ascontiguousarray(
                        np.repeat(plane[:, :, None], 3, axis=2))
                    surface = pygame.image.frombuffer(rgb, (shape[1], shape[0]), 'RGB')
                    if textures[index] is None:
                        textures[index] = Texture.from_surface(renderer, surface)
                    else:
                        textures[index].update(surface)
            now = time.monotonic()
            if textures[0] is None:
                if stop.is_set():
                    break
                time.sleep(.005)
                continue
            if chrome is None or now - chrome_at >= .25:
                if pretty:
                    chrome_surface = _chrome(
                        pygame, window.size, shape, layout, scanner,
                        smoothed_fps, refresh, mimicknet_available,
                        mimicknet_enabled)
                else:
                    redraw_hz = ((len(recent) - 1) / (recent[-1] - recent[0])
                                 if len(recent) > 1 else 0.)
                    chrome_surface = compact_font.render(
                        f'Pipeline {pipeline_fps:.1f} fps   Display {redraw_hz:.1f} Hz'
                        f'   Screen {refresh or "?"} Hz', True, (255, 230, 40),
                        BACKGROUND)
                chrome = Texture.from_surface(renderer, chrome_surface)
                chrome_at = now
            renderer.draw_color = (*BACKGROUND, 255)
            renderer.clear()
            image_rects = _image_rects(
                layout, comparison_available and mimicknet_enabled)
            textures[0].draw(dstrect=image_rects[0])
            if len(image_rects) == 2:
                (textures[1] or textures[0]).draw(dstrect=image_rects[1])
            if pretty:
                chrome.draw(dstrect=(0, 0, *window.size))
            else:
                chrome.draw(dstrect=(8, 8, chrome.width, chrome.height))
            renderer.present()
            presented = time.monotonic()
            presents += 1
            recent.append(presented)
            if first_present is None:
                first_present = presented
            last_present = presented
            if gray is not None:
                images += 1
                if first_image is None:
                    first_image = presented
                last_image = presented
            if stop.is_set():
                with lock:
                    if sequence == int(metadata[0]):
                        break
            # SDL may ignore requested vsync. Bound CPU use without halving
            # refresh on a working vsync renderer (present has already waited).
            remaining = 1 / (refresh or 60) - (presented - now)
            if remaining > .002:
                time.sleep(remaining)
        report = dict(screen_hz=refresh, vsync_requested=True, redraws=presents,
                      displayed_images=images, skipped_images=skipped,
                      redraw_hz=(presents - 1) / (last_present - first_present)
                      if presents > 1 else None,
                      new_image_fps=(images - 1) / (last_image - first_image)
                      if images > 1 else None,
                      mimicknet_available=mimicknet_available,
                      mimicknet_enabled=mimicknet_enabled,
                      mimicknet_toggles=mimicknet_toggles,
                      comparison=mimicknet_enabled and comparison_available,
                      smoothed_fps=smoothed_fps, pretty=pretty,
                      fullscreen=fullscreen, close_reason=close_reason,
                      window_size=window.size, image_size=layout['image'][2:],
                      measurement='Host after SDL present; redraws may repeat the latest image, not optical scanout.')
        textures = chrome = renderer = None
        window.destroy()
    except BaseException as exc:
        report = dict(error=f'{type(exc).__name__}: {exc}')
    finally:
        pygame.quit()
        closed.set()
        channel.send(dict(final=report))
        channel.close()


class Display:
    """One bounded shared display buffer; SDL stays off RX/GPU work."""
    def __init__(self, scale=.7, enabled=True, enhancement=None,
                 enhancement_enabled=False, snapshot=None, pretty=False):
        if enhancement_enabled and enhancement is None:
            raise ValueError('MimickNet cannot start enabled without an engine')
        self.scale, self.enabled, self.enhancement = scale, enabled, enhancement
        self.enhancement_enabled = bool(enhancement and enhancement_enabled)
        self.snapshot, self.pretty = snapshot, pretty
        self._comparison = bool(enabled and pretty and enhancement)
        self._process = None
        self._points = deque(maxlen=60)
        self._published = 0
        self.report = dict(enabled=enabled, mimicknet_available=enhancement is not None,
                           mimicknet_enabled=self.enhancement_enabled)

    def _start(self, shape, scanner):
        import numpy as np
        context = mp.get_context('spawn')
        planes = 2 if self._comparison else 1
        pixels = context.RawArray('B', planes * shape[0] * shape[1])
        shared_shape = (2, *shape) if self._comparison else shape
        self._pixels = np.frombuffer(pixels, np.uint8).reshape(shared_shape)
        self._metadata = context.RawArray('d', 2)
        self._lock = context.Lock()
        self._stop, self._closed = context.Event(), context.Event()
        self._channel, child = context.Pipe(duplex=False)
        self._process = context.Process(target=_viewer,
            args=(shape, self.scale, pixels, self._metadata, self._lock,
                  self._stop, self._closed, child, dict(scanner), self.pretty,
                  self.enhancement is not None, self.enhancement_enabled),
            name='ultrasound-viewer')
        self._process.start()
        child.close()
        if not self._channel.poll(10.):
            self.close()
            raise RuntimeError('Viewer startup timed out')
        self._messages()
        if not self.report.get('ready'):
            raise RuntimeError('Viewer did not become ready')

    def _messages(self):
        while self._channel.poll():
            try:
                message = self._channel.recv()
            except EOFError:
                break
            payload = message.get('final', message)
            if 'mimicknet_enabled' in payload:
                self.enhancement_enabled = bool(payload['mimicknet_enabled'])
            self.report.update(payload)
        if self.report.get('error'):
            raise RuntimeError(f'Viewer failed: {self.report["error"]}')

    def show(self, image_db, scanner=None):
        import cupy as cp
        if self._process is not None:
            self._messages()
        if self._comparison:
            enhanced = (self.enhancement(image_db)
                        if self.enhancement_enabled else image_db)
            shown = cp.stack((image_db, enhanced))
        else:
            shown = (self.enhancement(image_db)
                     if self.enhancement_enabled else image_db)
        if not self.enabled:
            cp.cuda.get_current_stream().synchronize()
            return True
        gray = cp.asnumpy((cp.clip((shown + 50.) / 50., 0., 1.) * 255).astype(cp.uint8))
        if self._process is None:
            self._start(gray.shape[-2:], {} if scanner is None else scanner)
        if self._closed.is_set():
            return False
        if not self._process.is_alive():
            raise RuntimeError('Viewer process exited unexpectedly')
        self._points.append(time.monotonic())
        fps = ((len(self._points) - 1) / (self._points[-1] - self._points[0])
               if len(self._points) > 1 else 0.)
        with self._lock:
            self._pixels[:] = gray
            self._published += 1
            self._metadata[:] = self._published, fps
        return True

    def close(self):
        if self._process is None:
            return
        self._stop.set()
        self._process.join(timeout=5.)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=2.)
            self.report['error'] = 'Viewer did not stop within five seconds'
        self.report['published_images'] = self._published
        try:
            self._messages()
            if self.snapshot is not None and self._published:
                from PIL import Image
                import numpy as np
                saved = (np.concatenate(self._pixels, axis=1)
                         if self._comparison and self.enhancement_enabled
                         else self._pixels[0] if self._comparison else self._pixels)
                Image.fromarray(saved).save(self.snapshot)
        finally:
            self._channel.close()
            self._process = None
