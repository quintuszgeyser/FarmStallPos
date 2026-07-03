"""
ReceiptRenderService
====================
Renders a sale receipt as a PIL Image at 203 DPI suitable for TSPL2 printing
on the XP-365B (or any thermal receipt printer).

Width is configurable via the 'receipt_width_mm' setting (default 72mm).
The XP-365B supports up to 82mm — use 72mm for standard 80mm roll with margins.

Returns (image, height_mm) so the TSPL2 SIZE command gets the correct height.
"""

import io
import os
import math
import logging
from typing import Tuple, Optional

log = logging.getLogger('pos')

DPI      = 203
MM_TO_PX = DPI / 25.4  # ≈ 7.99 px/mm


try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except ImportError:
    _PIL_OK = False


class ReceiptRenderService:

    # Characters per line at default font size on 72mm paper
    # Monospace font at ~7px wide → 72mm * 8px/mm = 576px / 7px ≈ 82 chars
    # We cap at 42 for readability.
    CHARS_PER_LINE = 42

    def render(self, receipt: dict, width_mm: float = 72) -> Tuple['Image', float]:
        """
        Render receipt dict to a PIL Image.
        Returns (image, height_mm).
        """
        if not _PIL_OK:
            raise RuntimeError('Pillow required: pip install Pillow')

        w_px = int(round(width_mm * MM_TO_PX))
        lines = self._build_lines(receipt, width_mm)

        font, font_h = self._load_font(w_px)
        line_h  = font_h + 4          # px between lines
        padding = int(4 * MM_TO_PX)   # 4mm top/bottom padding

        h_px      = padding * 2 + len(lines) * line_h + int(8 * MM_TO_PX)  # +8mm feed
        height_mm = h_px / MM_TO_PX

        img  = Image.new('RGB', (w_px, h_px), color='white')
        draw = ImageDraw.Draw(img)

        # Optional logo
        logo_file = receipt.get('logo_file', '')
        y = padding
        if logo_file:
            y = self._draw_logo(img, draw, logo_file, w_px, y)

        for line in lines:
            text    = line.get('text', '')
            align   = line.get('align', 'left')
            bold    = line.get('bold', False)
            divider = line.get('divider', False)

            if divider:
                draw.line([(0, y + line_h // 2), (w_px, y + line_h // 2)],
                          fill='black', width=1)
                y += line_h
                continue

            f = self._load_font(w_px, bold=bold)[0]
            if align == 'center':
                bbox = draw.textbbox((0, 0), text, font=f)
                tw   = bbox[2] - bbox[0]
                x    = max(0, (w_px - tw) // 2)
            elif align == 'right':
                bbox = draw.textbbox((0, 0), text, font=f)
                tw   = bbox[2] - bbox[0]
                x    = max(0, w_px - tw - 4)
            else:
                x = 4

            draw.text((x, y), text, fill='black', font=f)
            y += line_h

        return img, height_mm

    # ── Line builder ───────────────────────────────────────────────────────────

    def _build_lines(self, r: dict, width_mm: float) -> list:
        """Turn receipt dict into a flat list of {text, align, bold, divider} dicts."""
        chars = self._chars_per_line(width_mm)
        lines = []

        def add(text='', align='left', bold=False):
            lines.append({'text': text, 'align': align, 'bold': bold})

        def div():
            lines.append({'divider': True, 'text': ''})

        # Header
        store = r.get('store_name') or 'Farm Stall'
        legal = r.get('store_legal', '')
        add(store, align='center', bold=True)
        if legal and legal != store:
            add(legal, align='center')
        if r.get('vat_registered') and r.get('vat_number'):
            add(f"VAT No: {r['vat_number']}", align='center')
        add()

        # Date / receipt number
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(r['date_time']).strftime('%Y-%m-%d %H:%M')
        except Exception:
            dt = r.get('date_time', '')[:16]
        add(dt)
        add(f"Receipt: #{str(r['sale_id'])[:8]}")
        div()

        # Line items
        price_w = 9    # characters reserved for price column
        name_w  = chars - price_w - 1
        for ln in r.get('lines', []):
            name    = str(ln['name'])[:name_w]
            price   = f"R{ln['subtotal']:.2f}"
            # First row: name + price
            gap     = chars - len(name) - len(price)
            row     = name + ' ' * max(1, gap) + price
            add(row[:chars])
            # Second row: qty × unit_price if multi-qty
            if abs(ln['qty'] - 1.0) > 0.001:
                detail = f"  {ln['qty']:.3f} x R{ln['unit_price']:.2f}"
                add(detail[:chars])

        div()

        # Totals
        total_str = f"R{r['total']:.2f}"
        add('TOTAL'.ljust(chars - len(total_str)) + total_str, bold=True)

        if r.get('vat_registered'):
            vat_str = f"R{r['vat_amount']:.2f}"
            add(f"VAT ({r['vat_rate']:.0f}%)".ljust(chars - len(vat_str)) + vat_str)

        pm = (r.get('payment_method') or '').upper()
        if pm:
            add(f"Payment: {pm}")
        if r.get('cash_tendered'):
            tend_str = f"R{r['cash_tendered']:.2f}"
            add('Tendered'.ljust(chars - len(tend_str)) + tend_str)
        if r.get('change') and r['change'] > 0:
            chg_str = f"R{r['change']:.2f}"
            add('Change'.ljust(chars - len(chg_str)) + chg_str)

        div()

        # Footer
        footer = r.get('footer') or 'Thank you for your purchase!'
        for part in self._wrap(footer, chars):
            add(part, align='center')
        add()
        add()   # feed space

        return lines

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _chars_per_line(self, width_mm: float) -> int:
        # Monospace font: roughly 6px per char at 203 DPI on 72mm paper
        w_px = width_mm * MM_TO_PX
        return max(24, int(w_px / 6.5))

    def _load_font(self, w_px: int, bold: bool = False) -> tuple:
        # Font size: aim for ~42 chars across the paper
        font_px = max(14, int(w_px / 42))
        candidates_bold   = ['DejaVuSansMono-Bold.ttf', 'cour.ttf', 'courbd.ttf', 'arialbd.ttf']
        candidates_normal = ['DejaVuSansMono.ttf', 'DejaVuSans-Mono.ttf', 'cour.ttf', 'arial.ttf', 'Courier_New.ttf']
        candidates = candidates_bold if bold else candidates_normal
        from PIL import ImageFont
        for name in candidates:
            try:
                f = ImageFont.truetype(name, font_px)
                bbox = f.getbbox('W')
                return f, bbox[3] - bbox[1]
            except Exception:
                pass
        f = ImageFont.load_default()
        return f, 12

    def _draw_logo(self, img, draw, logo_file: str, w_px: int, y: int) -> int:
        base = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'branding')
        path = os.path.join(base, logo_file)
        if not os.path.exists(path):
            return y
        try:
            from PIL import Image as _Img
            logo = _Img.open(path).convert('RGBA')
            max_h = int(20 * MM_TO_PX)   # max 20mm logo height
            ratio = min(w_px / logo.width, max_h / logo.height)
            new_w = int(logo.width * ratio)
            new_h = int(logo.height * ratio)
            logo  = logo.resize((new_w, new_h), _Img.LANCZOS)
            x = (w_px - new_w) // 2
            img.paste(logo, (x, y), logo)
            return y + new_h + int(2 * MM_TO_PX)
        except Exception as e:
            log.debug('Receipt logo failed: %s', e)
            return y

    @staticmethod
    def _wrap(text: str, width: int) -> list:
        words  = text.split()
        lines  = []
        current = ''
        for word in words:
            if len(current) + len(word) + 1 <= width:
                current = (current + ' ' + word).strip()
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or ['']
