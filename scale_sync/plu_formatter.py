"""
PLU data formatting and product filtering for BC-4000 scale sync.

POS is the single source of truth. Scale is a downstream cache.
This module formats POS product data for the scale protocol.

MsgNo 1001: full PLU send — CSV with embedded Ishida description control codes
MsgNo 2023: delete PLU — payload: "{plu_no},"

Field sequence derived from decompiling SLP-V SlpDbServer.dll
SerializeScaleDataAc4000(), South Africa path (IsLongPluBc4000Country=True).
"""
import hashlib
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

logger = logging.getLogger('plu_formatter')

MSG_NO_FULL_PLU    = 1001  # confirmed from Wireshark 2026-06-23
MSG_NO_DELETE_PLU  = 26    # MsgNo 0026 status/delete session
MSG_NO_KEYBOARD    = 1024  # keyboard preset records
MSG_NO_ADVERT      = 1029  # advertisement message records

MAX_PLU_NO   = 99_999
MAX_NAME_LEN = 20


class ScaleSyncValidationError(Exception):
    pass


def validate_for_scale(product: dict):
    """Raise ScaleSyncValidationError if product cannot be safely sent to scale.
    POS is the master — any invalid state is a POS data problem, not a scale problem.
    """
    pid = product.get('id', '?')

    if not product.get('sync_to_scale'):
        raise ScaleSyncValidationError(f"Product {pid}: sync_to_scale is False")

    if product.get('is_archived'):
        raise ScaleSyncValidationError(f"Product {pid}: is archived")

    if not product.get('is_for_sale'):
        raise ScaleSyncValidationError(f"Product {pid}: not for sale")

    plu_no = product.get('product_code')
    if not plu_no:
        raise ScaleSyncValidationError(
            f"Product {pid} ('{product.get('name')}') has no product_code. "
            f"Cannot send to scale — would use database id which is wrong."
        )
    if plu_no <= 0 or plu_no > MAX_PLU_NO:
        raise ScaleSyncValidationError(
            f"Product {pid}: product_code {plu_no} out of range (1-{MAX_PLU_NO})"
        )

    if product.get('sold_by_weight'):
        ppu = product.get('price_per_unit')
        if not ppu or Decimal(str(ppu)) <= 0:
            raise ScaleSyncValidationError(f"Product {pid}: no valid price_per_unit")
    else:
        price = product.get('price')
        if not price or Decimal(str(price)) <= 0:
            raise ScaleSyncValidationError(f"Product {pid}: no valid price")

    name = (product.get('name') or '').strip()
    if not name:
        raise ScaleSyncValidationError(f"Product {pid}: empty name")


def should_sync(product: dict) -> bool:
    """Return True if product should be sent to scale. Logs reason if skipped."""
    try:
        validate_for_scale(product)
        return True
    except ScaleSyncValidationError as e:
        logger.debug(str(e))
        return False


def price_cents(product: dict) -> int:
    """Return scale price in whole cents.
    Weight items: price_per_unit (R/g) × 100000 = cents/kg
    Fixed items:  price (R) × 100 = cents
    """
    if product.get('sold_by_weight'):
        ppu = Decimal(str(product['price_per_unit']))
        return int((ppu * Decimal('100000')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    else:
        p = Decimal(str(product['price']))
        return int((p * Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def compute_scale_hash(product: dict) -> str:
    """Deterministic SHA-256 of scale-relevant product fields.
    If this hash matches scale_hash in the DB, the product is in sync.
    """
    tare_raw = product.get('scale_tare')
    parts = [
        str(product.get('product_code', '')),
        (product.get('name') or '').strip().upper()[:MAX_NAME_LEN],
        str(price_cents(product)),
        str(1 if product.get('sold_by_weight') else 0),
        str(float(tare_raw) if tare_raw is not None else 0),  # normalise Decimal/float → str
        str(product.get('scale_shelf_life') or 0),
        str(1 if product.get('scale_open_price') else 0),
        str((product.get('scale_msg1') or '').strip()[:20]),
        str((product.get('scale_msg2') or '').strip()[:20]),
        str(1 if product.get('scale_prohibit') else 0),
    ]
    return hashlib.sha256('|'.join(parts).encode()).hexdigest()


def format_prohibit_plu(plu_no: int) -> bytes:
    """Format a PLU as prohibited — price 0, description REMOVED, prohibit flag.
    Used for orphan removal (safer than zero-out or MsgNo 2023 delete).
    """
    desc = '\x0d\x0dREMOVED\x0d\x0d'
    f = [''] * 87
    for idx in [2,3,5,7,10,11,12,17,18,23,24,28,29,30,31,35,42,46,47,48,
                50,53,54,57,58,59,60,66,71,73,75,76,78,85,86]:
        f[idx] = '0'
    f[0]  = str(plu_no)
    f[1]  = str(plu_no)
    f[49] = f'"{desc}"'
    f[50] = '1'          # SalesMode fixed (safest for prohibited)
    f[51] = '0'
    f[52] = '0'
    f[56] = '1'
    f[61] = '0'
    f[67] = '0'          # BarCodeNum 0 = no barcode
    f[69] = '0'          # Posflag 0 = disabled
    f[70] = str(plu_no)
    return (','.join(f) + ',').encode('utf-8')


def format_full_plu(product: dict) -> bytes:
    """Format product as MsgNo 1001 CSV for BC-4000.

    Raises ScaleSyncValidationError if product_code is missing — no silent fallback
    to database id (that bug caused PLU 20 when product_code should have been 1).
    """
    validate_for_scale(product)

    # SalesMode / price consistency guard
    if product.get('sold_by_weight') and not product.get('price_per_unit'):
        raise ScaleSyncValidationError(f"Product {product.get('id')}: weight item missing price_per_unit")
    if not product.get('sold_by_weight') and not product.get('price'):
        raise ScaleSyncValidationError(f"Product {product.get('id')}: fixed item missing price")

    plu_no     = product['product_code']   # NEVER use product['id'] here
    sales_mode = 0 if product.get('sold_by_weight') else 1
    price      = price_cents(product)
    name       = (product.get('name') or '')[:MAX_NAME_LEN].upper().replace('"', '""')
    unit_line  = 'PER KG' if product.get('sold_by_weight') else 'EACH'
    # Description line separator is the LITERAL 4-char string "\x0d" (backslash-x-0-d,
    # LOWERCASE), NOT the control byte 0x0D. Confirmed from SLP-V's actual PLU WRITE
    # (MsgNo 1001) capture 2026-06-30, bytes 5c 78 30 64. (The scale 2001 readback
    # echoes it back uppercase, but writes must use lowercase to match SLP-V.)
    desc       = f'\\x0d\\x0d{name}\\x0d\\x0d{unit_line}'
    price_s    = str(price)

    # Confirmed field mappings (empirical — field_mapper.py + SLP-V DB readback):
    # CSV[9]  = ShelfLife (days)
    # CSV[28] = ExMessage1, CSV[29] = ExMessage2, CSV[30] = ExMessage3
    # CSV[61] = Tare (integer grams, no conversion)
    # CSV[67] = BarCodeNum, CSV[69] = Posflag
    barcode      = str(plu_no)   # ItemCode = PLU = product_code
    barcode_type = '21'          # BarCodeNum=21 (EAN enabled)
    pos_flag     = '20'          # Posflag=20
    tare         = str(int(float(product.get('scale_tare') or 0)))
    shelf_life   = str(product.get('scale_shelf_life') or 0)
    open_price   = '1' if product.get('scale_open_price') else '0'
    # Description: sanitize name (strip control chars, enforce 20 char limit)
    # Messages: append to label description as extra lines
    # Max 3 label lines: name + unit + (optional msg1) + (optional msg2)
    # Messages stored as text — append to description if set
    msg1_text  = (product.get('scale_msg1') or '').strip()[:20]
    msg2_text  = (product.get('scale_msg2') or '').strip()[:20]
    if msg1_text:
        desc += f'\\x0d\\x0d{msg1_text}'
    if msg2_text:
        desc += f'\\x0d\\x0d{msg2_text}'

    # 87-field layout captured from SLP-V network traffic (ground truth).
    # Empty string '' = reserved/unused field (not '0').
    # Positions confirmed via field_mapper.py + Wireshark capture.
    f = [''] * 87
    # Fields the scale sends as '0' — confirmed from 2001 readback of 6 live PLUs
    # (2026-06-30). Fields 18,23,24,42,46,47,48,85,86 are left EMPTY by the scale;
    # we previously sent '0' there, which malformed the record.
    for idx in [2,3,5,7,10,11,12,17,28,29,30,31,35,
                50,53,54,57,58,59,60,66,71,73,75,76,78]:
        f[idx] = '0'

    f[0]  = str(plu_no)       # PLU number = product_code
    f[1]  = str(plu_no)       # ItemCode = product_code
    f[9]  = shelf_life        # ShelfLife (days)
    f[45] = open_price        # OpenPrice flag (1=open, 0=fixed)
    f[49] = f'"{desc}"'       # Description
    f[50] = str(sales_mode)   # SalesMode (0=weight, 1=fixed)
    f[51] = price_s           # Price
    f[52] = price_s           # Price duplicate
    f[56] = '1'               # PackQuant (SLP-V always sends 1)
    f[61] = tare              # Tare (integer grams)
    f[67] = barcode_type      # BarCodeNum (21=EAN)
    f[69] = pos_flag          # Posflag (20=enabled)
    f[70] = str(plu_no)       # PosCode = product_code (unquoted string)
    fields = f

    return (','.join(fields) + ',').encode('utf-8')


def format_delete_plu(plu_no: int) -> bytes:
    """Format a PLU delete record for MsgNo 2023. Payload: '{plu_no},'"""
    return f"{plu_no},".encode('utf-8')
