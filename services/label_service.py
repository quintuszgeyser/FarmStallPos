"""
LabelRenderService + PrintDispatchService
==========================================
Rendering pipeline:
  Product data  ──┐
  LabelTemplate ──┤─→ LabelRenderService.render_png()  →  PNG (preview)
  Branding      ──┘─→ LabelRenderService.render_pdf()  →  PDF (print)

Print dispatch:
  PDF ──→ PrintDispatchService.send()
           ├─ usb        : raw PDF via browser WebUSB  (JS-side, server returns URL)
           ├─ bluetooth  : same via WebBluetooth
           └─ network    : HTTP POST to printer IP (future: CUPS / raw socket)

Barcode formats supported: EAN-13, Code128, Code39, QR

Label element types
-------------------
  product_name  | price | barcode | sku | store_name | store_logo
  weight        | category | custom_text
Each element has: { type, x, y, w, h, font_size?, align?, bold?, color?,
                    barcode_format?, value? (for custom_text) }
"""

import io
import os
import re
import json
import logging
from decimal import Decimal
from typing import Optional

log = logging.getLogger('pos')

# ── Optional heavy deps — fail gracefully so the app starts without them ──────
try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except ImportError:
    _PIL_OK = False
    log.warning('Pillow not installed — label PNG preview unavailable')

try:
    import barcode
    from barcode.writer import ImageWriter
    _BARCODE_OK = True
except ImportError:
    _BARCODE_OK = False
    log.warning('python-barcode not installed — barcode rendering unavailable')

try:
    import qrcode
    _QR_OK = True
except ImportError:
    _QR_OK = False


# Printer native resolution
PRINT_DPI = 203

# Render at 3× printer DPI for sharp screen preview and crisp print raster.
# At 203 DPI a 40mm label is only 320px wide — fonts look jaggy.
# At 609 DPI the same label is 960px — fonts and barcodes are sharp.
# The TSPL2 bitmap is sent at full resolution; the printer scales to its 203 DPI dots.
DPI      = PRINT_DPI * 3    # 609 DPI
MM_TO_PX = DPI / 25.4       # ≈ 24 px/mm


class LabelRenderService:
    """
    Converts a LabelTemplate dict + a Product ORM row into a PNG or PDF.

    All coordinates in the template elements are in mm (relative to label origin).
    The renderer works at RENDER_DPI internally, then can scale to any output size.
    """

    RENDER_DPI = DPI   # render at high DPI for sharpness; TSPL2 maps to printer dots

    def __init__(self, branding: dict):
        self.branding = branding

    # ── Public API ─────────────────────────────────────────────────────────────

    def render_png(self, template: dict, product=None, dpr: int = 1) -> bytes:
        """Return PNG bytes sized for the screen's physical pixel density (dpr=1..3)."""
        img = self.render_image(template, product)
        if dpr > 1:
            w, h = img.size
            img  = img.resize((w * dpr, h * dpr), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='PNG', dpi=(self.RENDER_DPI * dpr, self.RENDER_DPI * dpr))
        return buf.getvalue()

    def render_image(self, template: dict, product=None) -> 'Image':
        """
        Render one label as a PIL Image at 203 DPI.
        This is the canonical render output — used for both preview (→PNG)
        and print (→ESC/POS raster).  Removes the PDF layer entirely.
        """
        if not _PIL_OK:
            raise RuntimeError('Pillow is required. pip install Pillow')
        w_px = int(round(template['width_mm']  * MM_TO_PX))
        h_px = int(round(template['height_mm'] * MM_TO_PX))
        img  = Image.new('RGB', (w_px, h_px), color=template.get('background_color', '#ffffff'))
        draw = ImageDraw.Draw(img)
        if template.get('border'):
            draw.rectangle([0, 0, w_px - 1, h_px - 1], outline='#000000', width=2)
        context = self._build_context(product)
        for el in template.get('elements', []):
            self._draw_element_pil(draw, img, el, context, w_px, h_px)
        return img

    # ── Context builder ────────────────────────────────────────────────────────

    def _build_context(self, product) -> dict:
        """Flatten product + branding into a flat string-lookup dict."""
        ctx = {
            'store_name': self.branding.get('store_name', ''),
            'store_logo': self.branding.get('logo_file', ''),
        }
        if product:
            ctx['product_name'] = product.name or ''
            ctx['sku']          = str(product.product_code or product.id)
            ctx['barcode']      = product.barcode or ''
            ctx['category']     = getattr(product, 'category', None) and product.category.name or ''

            if product.sold_by_weight and product.price_per_unit:
                unit = 'kg' if (product.unit_type or '') == 'weight' else 'L'
                ctx['price'] = f'R{float(product.price_per_unit):.2f}/{unit}'
                ctx['weight'] = f'{product.base_unit or ""}'
            elif product.price is not None:
                ctx['price'] = f'R{float(product.price):.2f}'
                ctx['weight'] = ''
            else:
                ctx['price'] = ''
                ctx['weight'] = ''
        return ctx

    # ── PIL drawing ───────────────────────────────────────────────────────────

    def _draw_element_pil(self, draw, img, el, ctx, w_px, h_px):
        el_type = el.get('type', '')
        # Element bounding box in pixels
        ex = int(round(el.get('x', 0) * MM_TO_PX))
        ey = int(round(el.get('y', 0) * MM_TO_PX))
        ew = int(round(el.get('w', 20) * MM_TO_PX))
        eh = int(round(el.get('h', 8)  * MM_TO_PX))
        color = el.get('color', '#000000')

        if el_type == 'store_logo':
            logo_path = _resolve_logo(self.branding.get('logo_file', ''))
            if logo_path and os.path.exists(logo_path):
                try:
                    logo = Image.open(logo_path).convert('RGBA')
                    logo.thumbnail((ew, eh), Image.Resampling.LANCZOS)
                    img.paste(logo, (ex, ey), logo)
                except Exception as e:
                    log.debug('Logo paste failed: %s', e)
            return

        if el_type == 'barcode':
            bc_img = self._render_barcode_pil(ctx.get('barcode', ''), el, ew, eh)
            if bc_img:
                img.paste(bc_img, (ex, ey))
            return

        # ── Text rendering ────────────────────────────────────────────────────
        text = _resolve_text(el_type, el, ctx)
        if not text:
            return

        # font_size is in typographic points. Convert to pixels at render DPI.
        # 1 pt = 1/72 inch → px = pt × DPI / 72
        pt    = max(4, el.get('font_size', 9))
        px_sz = max(6, int(round(pt * DPI / 72)))
        bold  = el.get('bold', False)
        font  = _load_font(px_sz, bold)

        # Measure text so we can apply alignment within the element box
        bbox = draw.textbbox((0, 0), text, font=font)
        tw   = bbox[2] - bbox[0]   # text width in px
        th   = bbox[3] - bbox[1]   # text height in px

        align = el.get('align', 'left')
        if align == 'center':
            tx = ex + max(0, (ew - tw) // 2)
        elif align == 'right':
            tx = ex + max(0, ew - tw)
        else:
            tx = ex

        # Vertically centre text within the element box
        ty = ey + max(0, (eh - th) // 2)

        # Clip drawing to the element bounding box to avoid overflow
        draw.text((tx, ty), text, fill=color, font=font)

    def _render_barcode_pil(self, value: str, el: dict, w_px: int, h_px: int):
        """Render a barcode at high resolution then downsample — crisp at any size."""
        if not value:
            return None
        fmt = _pick_barcode_format(value, el.get('barcode_format', 'auto'))

        if fmt == 'qrcode':
            if not _QR_OK:
                return None
            # Render at 10× then downscale — sharp QR at any size
            scale = max(1, min(w_px, h_px) // 21)
            qr = qrcode.QRCode(
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=scale, border=1,
            )
            qr.add_data(value)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color='black', back_color='white').convert('RGB')
            return qr_img.resize((w_px, h_px), Image.Resampling.LANCZOS)

        if not _BARCODE_OK:
            return None
        try:
            # Render at 4× target resolution for sharp bars, then downscale
            scale = 4
            opts = {
                'write_text': False,
                'module_height': (h_px * scale) / DPI * 25.4,   # mm at scale DPI
                'module_width':  0.8,
                'quiet_zone':    2,
                'dpi':           DPI * scale,
            }
            BC     = barcode.get_barcode_class(fmt)
            writer = ImageWriter()
            buf    = io.BytesIO()
            BC(value, writer=writer).write(buf, options=opts)
            buf.seek(0)
            hi_res = Image.open(buf).convert('RGB')
            return hi_res.resize((w_px, h_px), Image.Resampling.LANCZOS)
        except Exception as e:
            log.debug('Barcode render failed (%s %s): %s', fmt, value, e)
            return None



class PrintDispatchService:
    """
    Sends a rendered PIL Image to the XP-365B using native TSPL2 commands.

    The XP-365B speaks TSPL2 (confirmed from official PPD: CMD:TSPL2,
    cupsFilter rastertosnailtspl-xprinter). We generate raw TSPL2 with
    an inline BITMAP payload and write directly to the printer — no CUPS,
    no python-escpos, no print dialog, no scaling, exact label size every time.

    Dispatch chain (tried in order):
      1. TSPL2 → pyusb  (USB direct, primary — no driver needed on Linux)
      2. TSPL2 → TCP socket port 9100  (network printers)
      3. TSPL2 → RFCOMM Bluetooth socket  (BT MAC address)
      4. TSPL2 → /dev/usb/lp* character device  (Linux lp fallback)
      5. PNG   → CUPS lp subprocess  (requires Xprinter driver on host)

    USB vendor IDs from official Xprinter Linux driver postinst quirks:
      0x2D84 (most XP-365B units)  |  0x2D37 (some batches)
    Run: lsusb | grep -iE "2d84|2d37"  to confirm.
    """

    XPRINTER_VIDS = [0x2D84, 0x2D37]

    def send(self, image, printer_id=None, width_mm: float = 40,
             height_mm: float = 20) -> dict:
        printer_row = db_get_printer(printer_id)
        tspl = self._build_tspl2(image, width_mm, height_mm)

        try:
            return self._send_usb(tspl, printer_row)
        except _PrinterNotFound:
            pass
        except Exception as e:
            log.warning('TSPL2 USB failed, trying fallbacks: %s', e)

        if printer_row and printer_row.connection == 'network' and printer_row.address:
            try:
                return self._send_network(tspl, printer_row)
            except Exception as e:
                log.warning('Network TSPL2 failed: %s', e)

        if printer_row and printer_row.connection == 'bluetooth' and printer_row.address:
            try:
                return self._send_bluetooth(tspl, printer_row)
            except Exception as e:
                log.warning('Bluetooth TSPL2 failed: %s', e)

        try:
            return self._send_lp_device(tspl)
        except Exception as e:
            log.warning('lp0 fallback failed: %s', e)

        try:
            return self._send_cups(image, width_mm, height_mm, printer_row)
        except Exception as e:
            log.warning('CUPS fallback failed: %s', e)

        raise RuntimeError(
            'Could not reach the printer. '
            'Check USB connection and power. '
            'On the server run: lsusb | grep -iE "2d84|2d37"'
        )

    # ── TSPL2 command builder ──────────────────────────────────────────────────

    def _build_tspl2(self, image, width_mm: float, height_mm: float) -> bytes:
        """
        Convert PIL Image to a complete TSPL2 print job.
        BITMAP x,y,bytes_per_row,height_dots,mode,<binary data>
        mode 1 = overwrite.  Bit=1 → black dot (MSB first per byte).
        """
        bw     = image.convert('1')
        w_dots = bw.width
        h_dots = bw.height
        bpr    = (w_dots + 7) // 8

        pixels = bw.load()
        bitmap = bytearray(bpr * h_dots)
        for y in range(h_dots):
            row = y * bpr
            for x in range(w_dots):
                if pixels[x, y] == 0:   # black pixel in PIL mode '1'
                    bitmap[row + x // 8] |= (0x80 >> (x % 8))

        header = (
            f'SIZE {width_mm:.1f} mm, {height_mm:.1f} mm\r\n'
            f'GAP 3 mm, 0 mm\r\n'
            f'DIRECTION 0\r\n'
            f'REFERENCE 0,0\r\n'
            f'OFFSET 0 mm\r\n'
            f'SET PEEL OFF\r\n'
            f'CLS\r\n'
            f'BITMAP 0,0,{bpr},{h_dots},1,'
        ).encode('ascii')
        footer = b'\r\nPRINT 1,1\r\n'
        return header + bytes(bitmap) + footer

    # ── USB direct via pyusb ───────────────────────────────────────────────────

    def _send_usb(self, tspl: bytes, printer_row) -> dict:
        try:
            import usb.core, usb.util
        except ImportError:
            raise _PrinterNotFound('pyusb not installed')

        vid, pid = self._resolve_vid_pid(printer_row)
        dev = None
        if vid and pid:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
        else:
            for v in self.XPRINTER_VIDS:
                dev = usb.core.find(idVendor=v)
                if dev:
                    break

        if dev is None:
            raise _PrinterNotFound('Xprinter USB device not found')

        if dev.is_kernel_driver_active(0):
            try:
                dev.detach_kernel_driver(0)
            except Exception:
                pass
        dev.set_configuration()

        cfg  = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        ep   = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
                                   == usb.util.ENDPOINT_OUT
        )
        if ep is None:
            raise RuntimeError('No bulk OUT endpoint on printer')

        for i in range(0, len(tspl), 64):
            ep.write(tspl[i:i + 64], timeout=5000)

        usb.util.dispose_resources(dev)
        return {'status': 'sent', 'notes': f'TSPL2 USB 0x{dev.idVendor:04x}:0x{dev.idProduct:04x}'}

    # ── Network raw TCP port 9100 ──────────────────────────────────────────────

    def _send_network(self, tspl: bytes, printer_row) -> dict:
        import socket
        host, _, port_s = printer_row.address.partition(':')
        port = int(port_s) if port_s else 9100
        with socket.create_connection((host, port), timeout=10) as s:
            s.sendall(tspl)
        return {'status': 'sent', 'notes': f'TSPL2 TCP {printer_row.address}'}

    # ── Bluetooth RFCOMM ───────────────────────────────────────────────────────

    def _send_bluetooth(self, tspl: bytes, printer_row) -> dict:
        import socket
        BTPROTO_RFCOMM = 3
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
        s.settimeout(15)
        s.connect((printer_row.address, 1))
        s.sendall(tspl)
        s.close()
        return {'status': 'sent', 'notes': f'TSPL2 BT {printer_row.address}'}

    # ── /dev/usb/lp* character device ─────────────────────────────────────────

    def _send_lp_device(self, tspl: bytes) -> dict:
        for dev_path in ['/dev/usb/lp0', '/dev/usb/lp1']:
            if os.path.exists(dev_path):
                with open(dev_path, 'wb') as f:
                    f.write(tspl)
                return {'status': 'sent', 'notes': f'TSPL2 {dev_path}'}
        raise RuntimeError('No /dev/usb/lp* device found')

    # ── CUPS lp subprocess (requires Xprinter driver on host) ─────────────────

    def _send_cups(self, image, width_mm: float, height_mm: float, printer_row) -> dict:
        import subprocess, tempfile
        printer_name = (printer_row.name if printer_row else None) or 'XP-365B'
        w_pt = round(width_mm  / 0.3528)
        h_pt = round(height_mm / 0.3528)
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            image.save(tmp.name, format='PNG', dpi=(203, 203))
            tmp_path = tmp.name
        try:
            result = subprocess.run(
                ['lp', '-d', printer_name,
                 '-o', f'PageSize=Custom.{w_pt}x{h_pt}pt',
                 '-o', 'fit-to-page=false',
                 tmp_path],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(f'lp failed: {result.stderr.strip()}')
            return {'status': 'sent', 'notes': f'CUPS lp {printer_name}'}
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _resolve_vid_pid(self, printer_row):
        if not printer_row or not printer_row.address:
            return None, None
        addr = (printer_row.address or '').strip()
        if ':' in addr and len(addr) <= 9:
            parts = addr.split(':')
            try:
                return int(parts[0], 16), int(parts[1], 16)
            except ValueError:
                pass
        return None, None


class _PrinterNotFound(Exception):
    pass


# ── Pure helpers ───────────────────────────────────────────────────────────────

def _load_font(px_size: int, bold: bool = False):
    """
    Load a truetype font at px_size pixels. Tries common paths on Ubuntu + Windows.
    Falls back to Pillow's built-in bitmap font (always available, low quality).
    """
    # Prefer DejaVu — ships with Ubuntu, metrically correct, free
    candidates = (
        [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
            'arialbd.ttf', 'Arial_Bold.ttf',
        ] if bold else [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
            'arial.ttf', 'Arial.ttf',
        ]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, px_size)
        except Exception:
            pass
    # Last resort — Pillow default bitmap font (ignores px_size)
    return ImageFont.load_default()


def _pick_barcode_format(value: str, hint: str) -> str:
    """Choose the best barcode format for a given value."""
    if hint and hint != 'auto':
        return hint.lower().replace('-', '')
    # EAN-13: 12 or 13 digits
    if re.fullmatch(r'\d{12,13}', value):
        return 'ean13'
    # EAN-8: 7 or 8 digits
    if re.fullmatch(r'\d{7,8}', value):
        return 'ean8'
    # QR: if it contains a URL or is very long
    if value.startswith('http') or len(value) > 30:
        return 'qrcode'
    # Fallback: Code128 — handles any ASCII
    return 'code128'


def _resolve_text(el_type: str, el: dict, ctx: dict) -> str:
    mapping = {
        'product_name': 'product_name',
        'price':        'price',
        'sku':          'sku',
        'store_name':   'store_name',
        'weight':       'weight',
        'category':     'category',
        'custom_text':  None,
    }
    if el_type == 'custom_text':
        return el.get('value', '')
    key = mapping.get(el_type)
    return ctx.get(key, '') if key else ''


def _hex_to_rgb(hex_color: str):
    h = hex_color.lstrip('#')
    if len(h) == 3:
        h = h[0]*2 + h[1]*2 + h[2]*2
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r/255, g/255, b/255


def _resolve_logo(logo_file: str) -> Optional[str]:
    if not logo_file:
        return None
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'branding')
    path = os.path.join(base, logo_file)
    return path if os.path.exists(path) else None


def db_get_printer(printer_id):
    if not printer_id:
        return None
    from models import LabelPrinter
    try:
        return LabelPrinter.query.filter_by(id=int(printer_id), is_active=True).first()
    except Exception:
        return None
