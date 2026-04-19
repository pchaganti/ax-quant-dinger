"""
USDT Payment Service (方案B：每单独立地址 + 自动对账)

MVP: USDT-TRC20（TronGrid），watch-only xpub 派生地址。
"""

import os
import threading
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.utils.db import get_db_connection
from app.utils.logger import get_logger
from app.services.billing_service import get_billing_service

logger = get_logger(__name__)


class UsdtPaymentService:
    # Class-level cache: only run DDL once per process to avoid taking schema
    # locks on every request (also prevents long-held txns when the DB is busy).
    _schema_ensured: bool = False

    def __init__(self):
        self.billing = get_billing_service()

    # -------------------- Config --------------------

    def _get_cfg(self) -> Dict[str, Any]:
        return {
            "enabled": str(os.getenv("USDT_PAY_ENABLED", "False")).lower() in ("1", "true", "yes"),
            "chain": (os.getenv("USDT_PAY_CHAIN", "TRC20") or "TRC20").upper(),
            "xpub_trc20": (os.getenv("USDT_TRC20_XPUB", "") or "").strip(),
            "trongrid_base": (os.getenv("TRONGRID_BASE_URL", "https://api.trongrid.io") or "").strip().rstrip("/"),
            "trongrid_key": (os.getenv("TRONGRID_API_KEY", "") or "").strip(),
            "usdt_trc20_contract": (os.getenv("USDT_TRC20_CONTRACT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t") or "").strip(),
            "confirm_seconds": int(float(os.getenv("USDT_PAY_CONFIRM_SECONDS", "30") or 30)),
            "order_expire_minutes": int(float(os.getenv("USDT_PAY_EXPIRE_MINUTES", "30") or 30)),
            "debug_reconcile_log": str(os.getenv("USDT_PAY_DEBUG_LOG", "true")).lower() in ("1", "true", "yes"),
            "trongrid_page_limit": min(200, max(1, int(float(os.getenv("USDT_TRONGRID_PAGE_LIMIT", "200") or 200)))),
            "trongrid_max_pages": max(1, min(20, int(float(os.getenv("USDT_TRONGRID_MAX_PAGES", "5") or 5)))),
        }

    # -------------------- Schema --------------------

    def _ensure_schema_best_effort(self, cur):
        """Best-effort create table/columns for old databases.

        Runs at most once per process (cached on the class) to avoid repeatedly
        taking schema-level locks inside request/worker transactions, which on
        a busy system contributes to `skipping vacuum --- lock not available`
        and `idle in transaction` pressure.
        """
        if UsdtPaymentService._schema_ensured:
            return
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS qd_usdt_orders (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
                    plan VARCHAR(20) NOT NULL,
                    chain VARCHAR(20) NOT NULL DEFAULT 'TRC20',
                    amount_usdt DECIMAL(20,6) NOT NULL DEFAULT 0,
                    address_index INTEGER NOT NULL DEFAULT 0,
                    address VARCHAR(80) NOT NULL DEFAULT '',
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    tx_hash VARCHAR(120) DEFAULT '',
                    paid_at TIMESTAMP,
                    confirmed_at TIMESTAMP,
                    expires_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
                """
            )
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_usdt_orders_address_unique ON qd_usdt_orders(chain, address)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_usdt_orders_user_id ON qd_usdt_orders(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_usdt_orders_status ON qd_usdt_orders(status)")
            UsdtPaymentService._schema_ensured = True
        except Exception:
            pass

    # -------------------- Address derivation --------------------

    def _derive_trc20_address_from_xpub(self, xpub: str, index: int) -> str:
        """
        Derive TRON address from xpub.

        Requires bip_utils.
        NOTE:
        - Some wallets export account-level xpub at m/44'/195'/0' (level=3).
        - Some export change-level xpub at m/44'/195'/0'/0 (level=4, external chain).
        This function supports both by normalizing to change-level before AddressIndex().
        """
        try:
            from bip_utils import Bip44, Bip44Coins, Bip44Changes
        except Exception as e:
            raise RuntimeError(f"bip_utils_missing:{e}")

        if not xpub:
            raise RuntimeError("missing_xpub")
        if index < 0:
            raise RuntimeError("invalid_index")

        ctx = Bip44.FromExtendedKey(xpub, Bip44Coins.TRON)
        lvl = int(ctx.Level())
        # Normalize to change-level (external chain) so we can derive addresses by index
        if lvl == 3:
            # account-level xpub: m/44'/195'/0'
            ctx = ctx.Change(Bip44Changes.CHAIN_EXT)
        elif lvl == 4:
            # change-level xpub: m/44'/195'/0'/0
            pass
        elif lvl == 5:
            # address-level xpub: cannot derive other indexes
            if index != 0:
                raise RuntimeError("xpub_is_address_level")
            return ctx.PublicKey().ToAddress()
        else:
            raise RuntimeError(f"unsupported_xpub_level:{lvl}")

        addr = ctx.AddressIndex(index).PublicKey().ToAddress()
        return addr

    # -------------------- Orders --------------------

    def create_order(self, user_id: int, plan: str) -> Tuple[bool, str, Dict[str, Any]]:
        cfg = self._get_cfg()
        if not cfg["enabled"]:
            return False, "usdt_pay_disabled", {}
        if cfg["chain"] != "TRC20":
            return False, "unsupported_chain", {}
        plan = (plan or "").strip().lower()
        if plan not in ("monthly", "yearly", "lifetime"):
            return False, "invalid_plan", {}

        plans = self.billing.get_membership_plans()
        amount = Decimal(str(plans.get(plan, {}).get("price_usd") or 0))
        if amount <= 0:
            return False, "invalid_amount", {}

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=cfg["order_expire_minutes"])

        try:
            with get_db_connection() as db:
                cur = db.cursor()
                self._ensure_schema_best_effort(cur)

                cur.execute(
                    "SELECT COALESCE(MAX(address_index), -1) as max_idx FROM qd_usdt_orders WHERE chain = 'TRC20'"
                )
                max_idx = cur.fetchone().get("max_idx")
                next_idx = int(max_idx) + 1

                address = self._derive_trc20_address_from_xpub(cfg["xpub_trc20"], next_idx)

                cur.execute(
                    """
                    INSERT INTO qd_usdt_orders
                      (user_id, plan, chain, amount_usdt, address_index, address, status, expires_at, created_at, updated_at)
                    VALUES (?, ?, 'TRC20', ?, ?, ?, 'pending', ?, NOW(), NOW())
                    RETURNING id
                    """,
                    (user_id, plan, amount, next_idx, address, expires_at),
                )
                row = cur.fetchone() or {}
                order_id = row.get("id")
                db.commit()
                cur.close()

            return True, "success", {
                "order_id": order_id,
                "plan": plan,
                "chain": "TRC20",
                "amount_usdt": str(amount),
                "address": address,
                "expires_at": expires_at.isoformat(),
            }
        except Exception as e:
            logger.error(f"create_order failed: {e}", exc_info=True)
            return False, f"error:{str(e)}", {}

    def get_order(self, user_id: int, order_id: int, refresh: bool = True) -> Tuple[bool, str, Dict[str, Any]]:
        try:
            # Step 1: short read txn, release connection before any HTTP work
            with get_db_connection() as db:
                cur = db.cursor()
                self._ensure_schema_best_effort(cur)

                cur.execute(
                    """
                    SELECT id, user_id, plan, chain, amount_usdt, address_index, address, status, tx_hash,
                           paid_at, confirmed_at, expires_at, created_at, updated_at
                    FROM qd_usdt_orders
                    WHERE id = ? AND user_id = ?
                    """,
                    (order_id, user_id),
                )
                row = cur.fetchone()
                cur.close()

            if not row:
                return False, "order_not_found", {}

            # Step 2: optionally do chain check OUTSIDE the DB txn.  TronGrid HTTP
            # can take tens of seconds; holding a pool connection while waiting on
            # the network is what used to produce `idle in transaction` and
            # `skipping vacuum --- lock not available` on qd_usdt_orders.
            if refresh:
                logger.info(
                    "USDT get_order refresh order_id=%s user_id=%s status=%s",
                    order_id,
                    user_id,
                    (row.get("status") or ""),
                )
                try:
                    self._refresh_one_order_out_of_tx(row)
                except Exception as e:
                    logger.warning(f"get_order refresh (out-of-tx) failed order_id={order_id}: {e}")

                # Step 3: short read txn to return fresh state
                with get_db_connection() as db:
                    cur = db.cursor()
                    cur.execute(
                        """
                        SELECT id, user_id, plan, chain, amount_usdt, address_index, address, status, tx_hash,
                               paid_at, confirmed_at, expires_at, created_at, updated_at
                        FROM qd_usdt_orders
                        WHERE id = ? AND user_id = ?
                        """,
                        (order_id, user_id),
                    )
                    row = cur.fetchone()
                    cur.close()

            return True, "success", self._row_to_dict(row)
        except Exception as e:
            logger.error(f"get_order failed: {e}", exc_info=True)
            return False, f"error:{str(e)}", {}

    @staticmethod
    def _coerce_utc_datetime(val: Any) -> Optional[datetime]:
        """Parse DB/driver timestamps (datetime or ISO str) to timezone-aware UTC."""
        if val is None:
            return None
        if isinstance(val, datetime):
            dt = val
        elif isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            except Exception:
                return None
        else:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _row_to_dict(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "order_id": row.get("id"),
            "plan": row.get("plan"),
            "chain": row.get("chain"),
            "amount_usdt": str(row.get("amount_usdt") or 0),
            "address": row.get("address") or "",
            "status": row.get("status") or "",
            "tx_hash": row.get("tx_hash") or "",
            "paid_at": row.get("paid_at").isoformat() if row.get("paid_at") else None,
            "confirmed_at": row.get("confirmed_at").isoformat() if row.get("confirmed_at") else None,
            "expires_at": row.get("expires_at").isoformat() if row.get("expires_at") else None,
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        }

    # -------------------- Chain check --------------------

    def _refresh_order_in_tx(self, cur, row: Dict[str, Any]) -> None:
        """Check chain status for a single order and update in the current transaction."""
        cfg = self._get_cfg()
        status = (row.get("status") or "").lower()
        chain = (row.get("chain") or "").upper()
        order_id = row.get("id")
        now = datetime.now(timezone.utc)

        if chain != "TRC20":
            logger.info("USDT refresh skip order_id=%s reason=unsupported_chain chain=%s", order_id, chain)
            return
        if status not in ("pending", "paid", "expired"):
            logger.info("USDT refresh skip order_id=%s reason=bad_status status=%s", order_id, status)
            return

        address = row.get("address") or ""
        amount = Decimal(str(row.get("amount_usdt") or 0))
        if not address or amount <= 0:
            logger.info(
                "USDT refresh skip order_id=%s reason=bad_address_or_amount has_address=%s amount=%s",
                order_id,
                bool(address),
                amount,
            )
            return

        # --- For 'paid' status, skip chain query and just check confirm delay ---
        if status == "paid":
            self._try_confirm_paid_order(cur, row, cfg, now)
            return

        # --- pending / expired: match historical TRC20 transfers (balance irrelevant) ---
        tx, chain_note = self._find_trc20_usdt_incoming(address, amount, row.get("created_at"))
        if not tx and chain_note and (
            chain_note.startswith("trongrid_http=") or chain_note.startswith("trongrid_request_error:")
        ):
            logger.warning("USDT reconcile TronGrid error order_id=%s user_id=%s %s", order_id, row.get("user_id"), chain_note)
        if cfg.get("debug_reconcile_log"):
            logger.info(
                "USDT reconcile scan order_id=%s user_id=%s status=%s amount=%s addr=%s expires_at=%s note=%s",
                order_id,
                row.get("user_id"),
                status,
                amount,
                address,
                row.get("expires_at"),
                chain_note if not tx else f"matched_tx={tx.get('transaction_id')}",
            )
        if tx:
            tx_hash = tx.get("transaction_id") or ""
            paid_at = datetime.now(timezone.utc)
            cur.execute(
                "UPDATE qd_usdt_orders SET status = 'paid', tx_hash = ?, paid_at = ?, updated_at = NOW() "
                "WHERE id = ? AND status IN ('pending','expired')",
                (tx_hash, paid_at, order_id),
            )
            rc = getattr(cur, "rowcount", -1)
            if rc == 0:
                logger.warning(
                    "USDT reconcile paid UPDATE skipped (0 rows) order_id=%s tx=%s DB status=%s",
                    order_id,
                    tx_hash,
                    status,
                )

            # Try to confirm immediately if delay is satisfied
            confirm_sec = int(cfg.get("confirm_seconds") or 30)
            try:
                tx_ts = tx.get("block_timestamp")
                if tx_ts:
                    tx_time = datetime.fromtimestamp(int(tx_ts) / 1000.0, tz=timezone.utc)
                    if (now - tx_time).total_seconds() >= confirm_sec:
                        self._confirm_and_activate_in_tx(cur, order_id, row.get("user_id"), row.get("plan"), tx_hash)
                elif confirm_sec <= 0:
                    self._confirm_and_activate_in_tx(cur, order_id, row.get("user_id"), row.get("plan"), tx_hash)
            except Exception:
                pass
            return

        # No matching transfer yet: only pending orders can transition to expired
        if status == "pending":
            exp = self._coerce_utc_datetime(row.get("expires_at"))
            if exp is not None and exp <= now:
                cur.execute(
                    "UPDATE qd_usdt_orders SET status = 'expired', updated_at = NOW() WHERE id = ? AND status = 'pending'",
                    (order_id,),
                )
                if cfg.get("debug_reconcile_log"):
                    logger.info(
                        "USDT reconcile mark_expired order_id=%s user_id=%s (no matching transfer) chain_note=%s",
                        order_id,
                        row.get("user_id"),
                        chain_note,
                    )

    def _try_confirm_paid_order(self, cur, row: Dict[str, Any], cfg: Dict[str, Any], now: datetime) -> None:
        """For orders already in 'paid' status, check if confirm delay is met and activate."""
        confirm_sec = int(cfg.get("confirm_seconds") or 30)
        paid_at = row.get("paid_at")
        if paid_at:
            if isinstance(paid_at, str):
                try:
                    paid_at = datetime.fromisoformat(paid_at.replace("Z", "+00:00"))
                except Exception:
                    paid_at = None
            if paid_at and paid_at.tzinfo is None:
                paid_at = paid_at.replace(tzinfo=timezone.utc)
            if paid_at and (now - paid_at).total_seconds() >= confirm_sec:
                self._confirm_and_activate_in_tx(cur, row["id"], row.get("user_id"), row.get("plan"), row.get("tx_hash") or "")
                return
        # Fallback: if paid_at missing but confirm_sec <= 0, confirm now
        if confirm_sec <= 0:
            self._confirm_and_activate_in_tx(cur, row["id"], row.get("user_id"), row.get("plan"), row.get("tx_hash") or "")

    def _confirm_and_activate_in_tx(self, cur, order_id: int, user_id: int, plan: str, tx_hash: str) -> None:
        """Mark order as confirmed and activate membership. Idempotent: skips if already confirmed."""
        # --- Idempotency check: re-read current status ---
        try:
            cur.execute("SELECT status FROM qd_usdt_orders WHERE id = ?", (order_id,))
            current = cur.fetchone()
            if current and (current.get("status") or "").lower() == "confirmed":
                logger.debug(f"USDT order {order_id} already confirmed, skipping activation.")
                return
        except Exception:
            pass

        # Mark confirmed
        cur.execute(
            "UPDATE qd_usdt_orders SET status='confirmed', confirmed_at = NOW(), updated_at = NOW() WHERE id = ? AND status IN ('paid','pending')",
            (order_id,),
        )
        # Activate membership
        try:
            ok, msg, data = self.billing.purchase_membership(int(user_id), str(plan))
            logger.info(f"USDT activate membership: order={order_id} user={user_id} plan={plan} ok={ok} msg={msg}")
        except Exception as e:
            logger.error(f"USDT activate membership failed: order={order_id} err={e}", exc_info=True)

    def _find_trc20_usdt_incoming(
        self, address: str, amount_usdt: Decimal, created_at: Optional[Any]
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        Returns (transfer_dict_or_None, debug_note).
        debug_note explains why no row matched (HTTP errors, empty list, filters).
        """
        cfg = self._get_cfg()
        base = cfg["trongrid_base"]
        contract = cfg["usdt_trc20_contract"]
        address = (address or "").strip()
        page_limit = int(cfg.get("trongrid_page_limit") or 200)
        max_pages = int(cfg.get("trongrid_max_pages") or 5)

        url = f"{base}/v1/accounts/{address}/transactions/trc20"
        headers = {}
        if cfg["trongrid_key"]:
            headers["TRON-PRO-API-KEY"] = cfg["trongrid_key"]

        def _parse_created_at(raw: Optional[Any]) -> Tuple[Optional[datetime], Optional[str]]:
            if raw is None:
                return None, None
            if isinstance(raw, datetime):
                return raw, None
            if isinstance(raw, str):
                try:
                    return datetime.fromisoformat(raw.replace("Z", "+00:00")), None
                except Exception as e:
                    return None, f"created_at_unparsed:{type(raw).__name__}:{e}"
            return None, f"created_at_unsupported_type:{type(raw).__name__}"

        # TRC20 USDT has 6 decimals
        target = int((amount_usdt * Decimal("1000000")).to_integral_value())

        min_ts = None
        created_warn = None
        ct_parsed, warn = _parse_created_at(created_at)
        created_warn = warn
        if ct_parsed:
            ct = ct_parsed
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=timezone.utc)
            min_ts = int(ct.timestamp() * 1000) - 60_000

        wrong_to = 0
        before_order = 0
        underpaid = 0
        parse_err = 0
        total_scanned = 0
        pages_fetched = 0
        fingerprint: Optional[str] = None

        try:
            for _ in range(max_pages):
                params: Dict[str, Any] = {
                    "only_to": "true",
                    "limit": page_limit,
                    "contract_address": contract,
                    "only_confirmed": "true",
                }
                if fingerprint:
                    params["fingerprint"] = fingerprint

                resp = requests.get(url, params=params, headers=headers, timeout=15)
                if resp.status_code != 200:
                    body_head = (resp.text or "")[:200].replace("\n", " ")
                    return None, f"trongrid_http={resp.status_code} body_head={body_head!r}"

                data = resp.json() or {}
                items = data.get("data") or []
                pages_fetched += 1
                total_scanned += len(items)

                for it in items:
                    try:
                        if (it.get("to") or "").strip() != address:
                            wrong_to += 1
                            continue
                        bts = int(it.get("block_timestamp") or 0)
                        if min_ts and bts < min_ts:
                            before_order += 1
                            continue
                        val = int(it.get("value") or 0)
                        if val < target:
                            underpaid += 1
                            continue
                        return it, f"ok pages={pages_fetched} scanned={total_scanned}"
                    except Exception:
                        parse_err += 1
                        continue

                meta = data.get("meta") or {}
                fingerprint = meta.get("fingerprint") if isinstance(meta.get("fingerprint"), str) else None
                if not fingerprint or len(items) < page_limit:
                    break

            parts = [
                f"scanned_items={total_scanned}",
                f"pages={pages_fetched}",
                f"limit={page_limit}",
                f"contract={contract}",
                f"target_raw={target}",
                f"min_ts={min_ts}",
                f"wrong_to={wrong_to}",
                f"before_order={before_order}",
                f"underpaid={underpaid}",
                f"parse_err={parse_err}",
            ]
            if created_warn:
                parts.append(created_warn)
            if total_scanned == 0:
                parts.append("hint=no_rows_for_this_contract_or_address")
            return None, "no_match " + " ".join(parts)
        except Exception as e:
            return None, f"trongrid_request_error:{type(e).__name__}:{e}"

    # -------------------- Batch refresh (for worker) --------------------

    def refresh_all_active_orders(self) -> int:
        """
        Scan all pending/paid USDT orders and refresh their chain status.

        Each order is checked with a short read txn, then the HTTP call to
        TronGrid is performed OUTSIDE of any DB transaction, and final updates
        are applied in short write txns.  This keeps pool connections from
        sitting `idle in transaction` while TronGrid is slow/unreachable.

        Returns the number of orders whose status changed.
        """
        updated = 0
        cfg = self._get_cfg()
        try:
            # Load candidate rows in one short read txn and release the connection.
            with get_db_connection() as db:
                cur = db.cursor()
                self._ensure_schema_best_effort(cur)
                cur.execute(
                    """
                    SELECT id, user_id, plan, chain, amount_usdt, address_index, address, status, tx_hash,
                           paid_at, confirmed_at, expires_at, created_at, updated_at
                    FROM qd_usdt_orders
                    WHERE status IN ('pending', 'paid')
                    ORDER BY created_at ASC
                    LIMIT 100
                    """
                )
                rows = cur.fetchall() or []
                cur.close()

            logger.info(
                "USDT reconcile batch start rows=%s debug_log=%s pay_enabled=%s",
                len(rows),
                cfg.get("debug_reconcile_log"),
                cfg.get("enabled"),
            )

            for row in rows:
                order_id = row.get("id")
                old_status = (row.get("status") or "").lower()
                try:
                    self._refresh_one_order_out_of_tx(row)
                except Exception as e:
                    logger.debug(f"refresh_all: order {order_id} error: {e}")
                    continue

                # Check if status changed (short read, new connection)
                try:
                    with get_db_connection() as db:
                        cur = db.cursor()
                        cur.execute("SELECT status FROM qd_usdt_orders WHERE id = ?", (order_id,))
                        new_row = cur.fetchone()
                        cur.close()
                    new_status = (new_row.get("status") or "").lower() if new_row else old_status
                    if new_status != old_status:
                        updated += 1
                        logger.info(f"USDT order {order_id}: {old_status} -> {new_status}")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"refresh_all_active_orders error: {e}", exc_info=True)
        return updated

    # -------------------- Out-of-transaction refresh helpers --------------------

    def _refresh_one_order_out_of_tx(self, row: Dict[str, Any]) -> None:
        """Refresh a single order.  HTTP is done here; DB writes are in short
        txns only.  Never hold a DB connection while calling TronGrid.
        """
        cfg = self._get_cfg()
        status = (row.get("status") or "").lower()
        chain = (row.get("chain") or "").upper()
        order_id = row.get("id")
        now = datetime.now(timezone.utc)

        if chain != "TRC20":
            logger.debug("USDT refresh skip order_id=%s reason=unsupported_chain chain=%s", order_id, chain)
            return
        if status not in ("pending", "paid", "expired"):
            return

        address = row.get("address") or ""
        amount = Decimal(str(row.get("amount_usdt") or 0))
        if not address or amount <= 0:
            return

        # 'paid' just waits for confirm delay; no HTTP needed
        if status == "paid":
            confirm_sec = int(cfg.get("confirm_seconds") or 30)
            paid_at = self._coerce_utc_datetime(row.get("paid_at"))
            ready = False
            if paid_at and (now - paid_at).total_seconds() >= confirm_sec:
                ready = True
            elif not paid_at and confirm_sec <= 0:
                ready = True
            if ready:
                self._confirm_and_activate_short_tx(
                    order_id, row.get("user_id"), row.get("plan"), row.get("tx_hash") or ""
                )
            return

        # pending / expired: TronGrid HTTP *outside* any DB txn
        tx, chain_note = self._find_trc20_usdt_incoming(address, amount, row.get("created_at"))

        if not tx and chain_note and (
            chain_note.startswith("trongrid_http=") or chain_note.startswith("trongrid_request_error:")
        ):
            logger.warning(
                "USDT reconcile TronGrid error order_id=%s user_id=%s %s",
                order_id, row.get("user_id"), chain_note,
            )
        if cfg.get("debug_reconcile_log"):
            logger.info(
                "USDT reconcile scan order_id=%s user_id=%s status=%s amount=%s addr=%s expires_at=%s note=%s",
                order_id,
                row.get("user_id"),
                status,
                amount,
                address,
                row.get("expires_at"),
                chain_note if not tx else f"matched_tx={tx.get('transaction_id')}",
            )

        if tx:
            tx_hash = tx.get("transaction_id") or ""
            paid_at = datetime.now(timezone.utc)
            # Short write txn: mark paid
            try:
                with get_db_connection() as db:
                    cur = db.cursor()
                    cur.execute(
                        "UPDATE qd_usdt_orders SET status = 'paid', tx_hash = ?, paid_at = ?, updated_at = NOW() "
                        "WHERE id = ? AND status IN ('pending','expired')",
                        (tx_hash, paid_at, order_id),
                    )
                    db.commit()
                    cur.close()
            except Exception as e:
                logger.error(f"USDT mark_paid UPDATE failed order_id={order_id}: {e}")
                return

            confirm_sec = int(cfg.get("confirm_seconds") or 30)
            try:
                tx_ts = tx.get("block_timestamp")
                if tx_ts:
                    tx_time = datetime.fromtimestamp(int(tx_ts) / 1000.0, tz=timezone.utc)
                    if (now - tx_time).total_seconds() >= confirm_sec:
                        self._confirm_and_activate_short_tx(order_id, row.get("user_id"), row.get("plan"), tx_hash)
                elif confirm_sec <= 0:
                    self._confirm_and_activate_short_tx(order_id, row.get("user_id"), row.get("plan"), tx_hash)
            except Exception:
                pass
            return

        # No matching transfer yet: pending can transition to expired
        if status == "pending":
            exp = self._coerce_utc_datetime(row.get("expires_at"))
            if exp is not None and exp <= now:
                try:
                    with get_db_connection() as db:
                        cur = db.cursor()
                        cur.execute(
                            "UPDATE qd_usdt_orders SET status = 'expired', updated_at = NOW() "
                            "WHERE id = ? AND status = 'pending'",
                            (order_id,),
                        )
                        db.commit()
                        cur.close()
                except Exception as e:
                    logger.warning(f"USDT mark_expired UPDATE failed order_id={order_id}: {e}")

    def _confirm_and_activate_short_tx(self, order_id: int, user_id: int, plan: str, tx_hash: str) -> None:
        """Idempotently mark confirmed in a short txn, then activate membership
        (which opens its own DB connection).  Never does HTTP inside a txn.
        """
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                cur.execute("SELECT status FROM qd_usdt_orders WHERE id = ?", (order_id,))
                current = cur.fetchone()
                if current and (current.get("status") or "").lower() == "confirmed":
                    cur.close()
                    return
                cur.execute(
                    "UPDATE qd_usdt_orders SET status='confirmed', confirmed_at = NOW(), updated_at = NOW() "
                    "WHERE id = ? AND status IN ('paid','pending')",
                    (order_id,),
                )
                db.commit()
                cur.close()
        except Exception as e:
            logger.error(f"USDT confirm UPDATE failed order_id={order_id}: {e}")
            return

        # Membership activation opens its own connection; keep it outside the
        # confirm-txn above.
        try:
            ok, msg, _ = self.billing.purchase_membership(int(user_id), str(plan))
            logger.info(f"USDT activate membership: order={order_id} user={user_id} plan={plan} ok={ok} msg={msg}")
        except Exception as e:
            logger.error(f"USDT activate membership failed: order={order_id} err={e}", exc_info=True)


# ==================== Background Worker ====================

class UsdtOrderWorker:
    """
    Background thread that periodically scans pending/paid USDT orders
    and checks on-chain status via TronGrid API.

    This ensures that even if the user closes the browser after payment,
    the order will still be confirmed and membership activated.
    """

    def __init__(self, poll_interval_sec: float = 30.0):
        self.poll_interval_sec = float(poll_interval_sec)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pay_disabled_logged = False

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="UsdtOrderWorker", daemon=True)
            self._thread.start()
            logger.info("UsdtOrderWorker started (interval=%ss)", self.poll_interval_sec)
            return True

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            logger.info("UsdtOrderWorker stopped")

    def _run_loop(self):
        # Wait a bit on startup to let the app fully initialize
        self._stop_event.wait(timeout=10)

        while not self._stop_event.is_set():
            try:
                svc = get_usdt_payment_service()
                cfg = svc._get_cfg()
                if cfg["enabled"]:
                    updated = svc.refresh_all_active_orders()
                    if updated > 0:
                        logger.info(f"UsdtOrderWorker: refreshed {updated} orders")
                else:
                    if not self._pay_disabled_logged:
                        logger.info(
                            "UsdtOrderWorker: USDT_PAY_ENABLED is false — worker runs but skips "
                            "refresh_all_active_orders (set USDT_PAY_ENABLED=true to reconcile)."
                        )
                        self._pay_disabled_logged = True
            except Exception as e:
                logger.error(f"UsdtOrderWorker loop error: {e}", exc_info=True)

            self._stop_event.wait(timeout=self.poll_interval_sec)


# ==================== Singletons ====================

_svc = None
_worker = None


def get_usdt_payment_service() -> UsdtPaymentService:
    global _svc
    if _svc is None:
        _svc = UsdtPaymentService()
    return _svc


def get_usdt_order_worker() -> UsdtOrderWorker:
    global _worker
    if _worker is None:
        interval = float(os.getenv("USDT_WORKER_POLL_INTERVAL", "30"))
        _worker = UsdtOrderWorker(poll_interval_sec=interval)
    return _worker
