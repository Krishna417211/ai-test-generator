import { useEffect, useState, useCallback, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Search, Loader2, RefreshCw, ShieldAlert, Ban, CheckCircle2, Trash2, LogOut,
  X, ChevronLeft, ChevronRight, Github, Mail, MailX, Gauge, ArrowUp, ArrowDown,
} from "lucide-react";
import Page from "../components/Page";
import {
  fetchAdminUsers, fetchAdminUser, adminSuspendUser, adminSetPlan, adminSetUsage,
  adminLogoutUser, adminDeleteUser,
  type AdminUser, type AdminUserDetail, type AdminUserSort,
} from "../utils/api";
import { pluralize, formatDate, formatWhen, shortSource } from "../utils/format";

const PAGE_SIZE = 25;

/** Debounce the search box: a request per keystroke would race, and the later
 *  reply isn't guaranteed to be the later query. */
function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** The sortable columns, and the default direction each one gets on first click.
 *
 *  Descending-first for the flags and dates, because the reason you click
 *  "Joined" or "Status" is to see the newest or the suspended — putting those on
 *  page 1 is the whole point. Text sorts A→Z, which is what a name column is for.
 */
const SORTS: { key: AdminUserSort; label: string; firstDir: "asc" | "desc" }[] = [
  { key: "created_at", label: "Joined", firstDir: "desc" },
  { key: "email", label: "Email", firstDir: "asc" },
  { key: "name", label: "Name", firstDir: "asc" },
  { key: "plan", label: "Plan", firstDir: "desc" },
  { key: "suspended", label: "Status", firstDir: "desc" },
  { key: "email_verified", label: "Verified", firstDir: "desc" },
];

/** Sort controls. Not a <th> row — the list below is a card per account, not a
 *  table, so these are buttons with aria-pressed rather than faked headers. */
function SortBar({ sort, direction, onSort }: {
  sort: AdminUserSort;
  direction: "asc" | "desc";
  onSort: (key: AdminUserSort) => void;
}) {
  return (
    <div className="flex items-center gap-1 flex-wrap px-1" role="group" aria-label="Sort accounts">
      <span className="text-[10px] uppercase tracking-wider text-grey-500 pr-1.5">Sort</span>
      {SORTS.map(({ key, label }) => {
        const active = sort === key;
        const Arrow = direction === "asc" ? ArrowUp : ArrowDown;
        return (
          <button
            key={key}
            onClick={() => onSort(key)}
            aria-pressed={active}
            title={active ? `Sorted by ${label} — click to reverse` : `Sort by ${label}`}
            className={`flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] font-medium transition-colors ${
              active
                ? "text-white bg-white/10"
                : "text-grey-400 hover:text-white hover:bg-white/5"
            }`}
          >
            {label}
            {/* Only the active column shows an arrow: one on every column would
                imply they're all sorted, and give no cue which is in effect. */}
            {active && <Arrow size={11} aria-hidden />}
          </button>
        );
      })}
    </div>
  );
}

/** Placeholder rows shaped like real ones, so the list doesn't jump when data
 *  lands. Shown only on the first load — a refetch keeps the current rows
 *  dimmed instead, which is steadier than blanking a list you're reading. */
function RowSkeleton() {
  return (
    <div className="border-b border-grey-800 last:border-b-0 px-4 py-3 flex items-center gap-3">
      <div className="w-8 h-8 rounded-lg bg-white/[0.06] animate-pulse shrink-0" />
      <div className="flex-1 min-w-0 space-y-2">
        <div className="h-3 w-44 max-w-full rounded bg-white/[0.06] animate-pulse" />
        <div className="h-2.5 w-28 max-w-full rounded bg-white/[0.04] animate-pulse" />
      </div>
      <div className="h-4 w-10 rounded-full bg-white/[0.05] animate-pulse shrink-0" />
    </div>
  );
}

function Badge({ children, tone = "grey" }: {
  children: React.ReactNode;
  tone?: "grey" | "brand" | "amber" | "rose" | "emerald";
}) {
  const tones = {
    grey: "text-grey-300 border-grey-600 bg-white/[0.06]",
    brand: "text-brand-200 border-brand-400/40 bg-brand-400/10",
    amber: "text-amber-300 border-amber-500/40 bg-amber-500/10",
    rose: "text-rose-300 border-rose-500/40 bg-rose-500/10",
    emerald: "text-emerald-300 border-emerald-500/40 bg-emerald-500/10",
  };
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded-full border font-medium whitespace-nowrap ${tones[tone]}`}>
      {children}
    </span>
  );
}

/** The destructive-action gate: type the account's own email to arm the button.
 *  The server re-checks the same string, so this is a speed bump for the admin,
 *  not the actual guard. */
function DeleteConfirm({ user, onDone, onCancel }: {
  user: AdminUser; onDone: (msg: string) => void; onCancel: () => void;
}) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const expected = user.email || user.github_login || user.id;

  const go = async () => {
    setBusy(true);
    setError("");
    try {
      onDone(await adminDeleteUser(user.id, typed));
    } catch (e: any) {
      setError(e.message || "Could not delete that account.");
      setBusy(false);
    }
  };

  return (
    <div className="mt-3 rounded-xl border border-rose-500/40 bg-rose-500/[0.07] p-4">
      <div className="flex items-start gap-2 mb-3">
        <Trash2 size={13} className="text-rose-400 shrink-0 mt-0.5" />
        <div className="text-xs text-rose-200">
          <p className="font-medium">Delete {expected} permanently?</p>
          <p className="text-rose-300/80 mt-1">
            Their account, {pluralize(user.generations, "generation")}, {pluralize(user.scans, "scan")},
            publish history, and settings are erased. This cannot be undone.
          </p>
        </div>
      </div>
      <input
        value={typed}
        onChange={(e) => setTyped(e.target.value)}
        placeholder={`Type ${expected} to confirm`}
        className="w-full px-3 py-2 rounded-lg bg-ink-950/60 border border-grey-700 text-xs text-white placeholder:text-grey-600 focus:outline-none focus:border-rose-500/60"
      />
      {error && <p className="mt-2 text-[11px] text-rose-300">{error}</p>}
      <div className="flex items-center gap-2 mt-3">
        <button
          onClick={go}
          disabled={busy || typed.trim().toLowerCase() !== expected.toLowerCase()}
          className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-rose-500/90 text-white hover:bg-rose-500 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {busy ? "Deleting…" : "Delete permanently"}
        </button>
        <button onClick={onCancel} className="px-3 py-1.5 rounded-lg btn-ghost text-xs">
          Cancel
        </button>
      </div>
    </div>
  );
}

/** The expanded panel for one account: history, and everything an admin can do
 *  to it. Loaded on open rather than with the list — the list would otherwise
 *  fan out into a detail query per row. */
function UserPanel({ user, onChanged, onDeleted, onError }: {
  user: AdminUser;
  onChanged: (u: AdminUser) => void;
  onDeleted: (msg: string) => void;
  onError: (msg: string) => void;
}) {
  const [detail, setDetail] = useState<AdminUserDetail | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [reason, setReason] = useState("");

  useEffect(() => {
    let live = true;
    fetchAdminUser(user.id)
      .then((d) => { if (live) setDetail(d); })
      .catch((e) => onError(e.message));
    return () => { live = false; };
  }, [user.id]);

  /** Every mutation returns the updated row, so the list re-renders from the
   *  server's answer rather than a guess about what changed. */
  const act = async (label: string, fn: () => Promise<AdminUser>) => {
    setBusy(label);
    try {
      onChanged(await fn());
      setDetail(await fetchAdminUser(user.id));
    } catch (e: any) {
      onError(e.message || "That didn't work.");
    } finally {
      setBusy(null);
    }
  };

  const isSelfOrAdmin = user.is_admin;

  return (
    <div className="border-t border-grey-800 bg-ink-950/40 px-4 py-4">
      {isSelfOrAdmin && (
        <div className="mb-4 flex items-start gap-2 rounded-xl border border-amber-500/30 bg-amber-500/[0.07] px-3 py-2.5">
          <ShieldAlert size={13} className="text-amber-400 shrink-0 mt-0.5" />
          <p className="text-[11px] text-amber-200/90">
            This account is an admin (its address is in <code className="font-mono">ADMIN_EMAILS</code>).
            It can't be suspended or deleted from here — remove the address from the
            environment and restart first.
          </p>
        </div>
      )}

      <div className="grid md:grid-cols-3 gap-4">
        <div className="space-y-3">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-grey-500">
            Plan &amp; quota
          </div>
          <div className="rounded-xl border border-grey-700 bg-white/[0.02] p-3 space-y-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-grey-400">Plan</span>
              <Badge tone={user.plan === "pro" ? "brand" : "grey"}>
                {user.plan === "pro" ? "Pro" : "Free"}
              </Badge>
            </div>
            {user.plan_expires_at && (
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-grey-400">Expires</span>
                <span className="text-[11px] text-grey-300">{formatDate(user.plan_expires_at)}</span>
              </div>
            )}
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-grey-400">Used this month</span>
              <span className="text-[11px] text-grey-300 tabular-nums">
                {detail ? `${detail.quota.used}${detail.quota.limit !== null ? ` / ${detail.quota.limit}` : ""}` : "—"}
              </span>
            </div>
          </div>

          <div className="flex flex-wrap gap-1.5">
            {user.plan === "pro" ? (
              <button
                onClick={() => act("plan", () => adminSetPlan(user.id, "free", null, reason))}
                disabled={busy !== null}
                className="px-2.5 py-1.5 rounded-lg btn-ghost text-[11px] disabled:opacity-50"
              >
                {busy === "plan" ? "Working…" : "Downgrade to Free"}
              </button>
            ) : (
              <>
                {/* Comped Pro defaults to a term rather than forever: an
                    unbounded grant is the one that gets forgotten. */}
                <button
                  onClick={() => act("plan", () => adminSetPlan(user.id, "pro", 30, reason))}
                  disabled={busy !== null}
                  className="px-2.5 py-1.5 rounded-lg btn-ghost text-[11px] disabled:opacity-50"
                >
                  {busy === "plan" ? "Working…" : "Pro · 30 days"}
                </button>
                <button
                  onClick={() => act("plan", () => adminSetPlan(user.id, "pro", 365, reason))}
                  disabled={busy !== null}
                  className="px-2.5 py-1.5 rounded-lg btn-ghost text-[11px] disabled:opacity-50"
                >
                  Pro · 1 year
                </button>
              </>
            )}
            <button
              onClick={() => act("usage", () => adminSetUsage(user.id, 0))}
              disabled={busy !== null}
              className="px-2.5 py-1.5 rounded-lg btn-ghost text-[11px] disabled:opacity-50 flex items-center gap-1"
            >
              <Gauge size={11} /> {busy === "usage" ? "Working…" : "Reset usage"}
            </button>
          </div>
        </div>

        <div className="space-y-3">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-grey-500">
            Account
          </div>
          <div className="rounded-xl border border-grey-700 bg-white/[0.02] p-3 space-y-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-grey-400">Joined</span>
              <span className="text-[11px] text-grey-300">{formatDate(user.created_at)}</span>
            </div>
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-grey-400">Signed in</span>
              <span className="text-[11px] text-grey-300">
                {detail ? pluralize(detail.active_sessions, "session") : "—"}
              </span>
            </div>
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-grey-400">User ID</span>
              <span className="text-[10px] font-mono text-grey-500">{user.id}</span>
            </div>
          </div>

          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Reason (optional, logged)"
            className="w-full px-3 py-2 rounded-lg bg-ink-950/60 border border-grey-700 text-[11px] text-white placeholder:text-grey-600 focus:outline-none focus:border-grey-500"
          />

          <div className="flex flex-wrap gap-1.5">
            <button
              onClick={() => act("logout", async () => {
                await adminLogoutUser(user.id);
                return user;
              })}
              disabled={busy !== null}
              className="px-2.5 py-1.5 rounded-lg btn-ghost text-[11px] disabled:opacity-50 flex items-center gap-1"
            >
              <LogOut size={11} /> {busy === "logout" ? "Working…" : "Sign out everywhere"}
            </button>
            {!isSelfOrAdmin && (
              <>
                <button
                  onClick={() => act("suspend", () => adminSuspendUser(user.id, !user.suspended, reason))}
                  disabled={busy !== null}
                  className={`px-2.5 py-1.5 rounded-lg text-[11px] disabled:opacity-50 flex items-center gap-1 transition-colors ${
                    user.suspended
                      ? "border border-emerald-500/40 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20"
                      : "border border-amber-500/40 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20"
                  }`}
                >
                  {user.suspended ? <CheckCircle2 size={11} /> : <Ban size={11} />}
                  {busy === "suspend" ? "Working…" : user.suspended ? "Unsuspend" : "Suspend"}
                </button>
                <button
                  onClick={() => setConfirmingDelete((v) => !v)}
                  disabled={busy !== null}
                  className="px-2.5 py-1.5 rounded-lg border border-rose-500/40 bg-rose-500/10 text-rose-300 text-[11px] hover:bg-rose-500/20 disabled:opacity-50 flex items-center gap-1 transition-colors"
                >
                  <Trash2 size={11} /> Delete
                </button>
              </>
            )}
          </div>
        </div>

        <div className="space-y-3">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-grey-500">
            Recent work
          </div>
          {!detail ? (
            <div className="flex items-center gap-2 text-xs text-grey-500 py-3">
              <Loader2 size={12} className="animate-spin" /> Loading…
            </div>
          ) : detail.recent_generations.length === 0 && detail.recent_scans.length === 0 ? (
            <div className="rounded-xl border border-dashed border-grey-700 px-3 py-6 text-center text-[11px] text-grey-500">
              Nothing yet.
            </div>
          ) : (
            <div className="space-y-1.5 max-h-[220px] overflow-y-auto">
              {/* Composite keys: list_generations/list_scans project a display
                  row and don't include the primary key, so `g.id` is undefined
                  and every row would share it. */}
              {detail.recent_generations.map((g: any, i: number) => (
                <div key={`g-${g.created_at}-${i}`} className="rounded-lg border border-grey-800 bg-white/[0.02] px-2.5 py-2">
                  <div className="text-[11px] font-mono text-grey-300 truncate">
                    {shortSource(g.source || "")}
                  </div>
                  <div className="text-[10px] text-grey-500 mt-0.5">
                    {g.status !== "success" ? (
                      <span className="text-rose-400">Failed</span>
                    ) : (
                      <>{pluralize(g.test_count || 0, "test")}</>
                    )}
                    {" · "}{formatWhen(g.created_at)}
                  </div>
                </div>
              ))}
              {detail.recent_scans.map((s: any, i: number) => (
                <div key={`s-${s.created_at}-${i}`} className="rounded-lg border border-grey-800 bg-white/[0.02] px-2.5 py-2">
                  <div className="text-[11px] font-mono text-grey-300 truncate">
                    {shortSource(s.url || "")}
                  </div>
                  <div className="text-[10px] text-grey-500 mt-0.5">
                    Scan · grade {s.grade} · {formatWhen(s.created_at)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {confirmingDelete && !isSelfOrAdmin && (
        <DeleteConfirm
          user={user}
          onDone={onDeleted}
          onCancel={() => setConfirmingDelete(false)}
        />
      )}
    </div>
  );
}

export default function AdminUsers() {
  // The query lives in the URL so a link from the overview's failure list can
  // land straight on the account it names.
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState(params.get("q") || "");
  const debounced = useDebounced(query, 300);

  const [data, setData] = useState<{ users: AdminUser[]; total: number } | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [sort, setSort] = useState<AdminUserSort>("created_at");
  const [direction, setDirection] = useState<"asc" | "desc">("desc");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);

  // Guards against an out-of-order reply overwriting a newer one.
  const reqRef = useRef(0);

  const load = useCallback(async (
    q: string, off: number, s: AdminUserSort, dir: "asc" | "desc",
  ) => {
    const seq = ++reqRef.current;
    setLoading(true);
    try {
      const res = await fetchAdminUsers(q, PAGE_SIZE, off, s, dir);
      if (seq !== reqRef.current) return;
      setData({ users: res.users, total: res.total });
      setError(null);
    } catch (e: any) {
      if (seq === reqRef.current) setError(e.message || "Could not load users.");
    } finally {
      if (seq === reqRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(debounced, offset, sort, direction);
  }, [debounced, offset, sort, direction, load]);

  // Drop the selection whenever the visible set changes. A tick that survives
  // onto another page is a checkbox you can no longer see attached to an account
  // you've forgotten you picked — and the next click acts on it.
  useEffect(() => { setSelected(new Set()); }, [debounced, offset, sort, direction]);

  /** Clicking the active column reverses it; a new column starts at whichever
   *  direction makes that column useful (see SORTS.firstDir). */
  const onSort = (key: AdminUserSort) => {
    if (key === sort) {
      setDirection((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSort(key);
      setDirection(SORTS.find((s) => s.key === key)?.firstDir ?? "desc");
    }
    // Page 1: row 30 of the old order has nothing to do with row 30 of the new.
    setOffset(0);
    setOpenId(null);
  };

  // A new search starts at page 1 — keeping the offset would land on an empty
  // page whenever the new result set is shorter than the old one.
  useEffect(() => {
    setOffset(0);
    setParams(debounced ? { q: debounced } : {}, { replace: true });
  }, [debounced]);

  const patchUser = (u: AdminUser) =>
    setData((d) => d && { ...d, users: d.users.map((x) => (x.id === u.id ? u : x)) });

  const toggleSelect = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  // Admins are never selectable (the server refuses to suspend one anyway), so
  // "select all" means every row that could actually be acted on — otherwise the
  // header box would sit indeterminate forever on a page containing an admin.
  const selectableIds = (data?.users ?? []).filter((u) => !u.is_admin).map((u) => u.id);
  const allSelected = selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));
  const someSelected = selectableIds.some((id) => selected.has(id));

  const toggleSelectAll = () =>
    setSelected(allSelected ? new Set() : new Set(selectableIds));

  /** Apply suspend/unsuspend across the selection.
   *
   *  Deliberately N calls to the per-user endpoint rather than a bulk route: that
   *  endpoint already refuses admins, revokes the target's sessions, and returns
   *  the updated row. A bulk endpoint would have to re-implement all three, and
   *  any drift between them would be a privilege bug.
   *
   *  Sequential, not Promise.all: this is an admin acting on real accounts, and
   *  firing 25 writes at once at a single-instance SQLite box is how you get a
   *  half-applied batch and a locked database.
   */
  const bulkSuspend = async (suspend: boolean) => {
    const targets = (data?.users ?? []).filter((u) => selected.has(u.id) && !u.is_admin);
    if (!targets.length) return;
    setBulkBusy(true);
    setError(null);
    let ok = 0;
    const failed: string[] = [];
    for (const u of targets) {
      try {
        patchUser(await adminSuspendUser(u.id, suspend, "Bulk action from the admin console"));
        ok++;
      } catch {
        failed.push(u.email || u.id);
      }
    }
    setBulkBusy(false);
    setSelected(new Set());
    // Report what actually happened, including partial failure — claiming "done"
    // when 2 of 5 didn't apply is how an admin thinks an account is locked when
    // it isn't.
    // pluralize() already carries the count ("6 accounts"), so the total is not
    // repeated ahead of it.
    const verb = suspend ? "Suspended" : "Unsuspended";
    setNotice(`${verb} ${ok} of ${pluralize(targets.length, "account")}.`);
    if (failed.length) setError(`Couldn't update: ${failed.join(", ")}.`);
  };

  const removeUser = (id: string, msg: string) => {
    setData((d) => d && { ...d, users: d.users.filter((x) => x.id !== id), total: d.total - 1 });
    setOpenId(null);
    setNotice(msg);
  };

  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <Page className="pb-8">
      <div className="space-y-5">
        <div className="flex items-end justify-between gap-4 flex-wrap">
          <div>
            <h1 className="font-display text-2xl font-bold tracking-tight">Users</h1>
            <p className="text-sm text-grey-400 mt-1">
              {data ? pluralize(data.total, "account") : "…"}
              {debounced && data ? ` matching “${debounced}”` : " on this instance"}
            </p>
          </div>
          <button
            onClick={() => load(debounced, offset, sort, direction)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg btn-ghost text-xs text-grey-300"
          >
            <RefreshCw size={12} /> Refresh
          </button>
        </div>

        <div className="relative">
          <Search size={14} className="absolute left-3.5 top-1/2 -translate-y-1/2 text-grey-500 pointer-events-none" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search by email, name, or GitHub handle…"
            aria-label="Search users"
            className="w-full pl-10 pr-9 py-2.5 rounded-xl glass-strong border border-grey-700 text-sm text-white placeholder:text-grey-600 focus:outline-none focus:border-grey-500"
          />
          {query && (
            <button
              onClick={() => setQuery("")}
              aria-label="Clear search"
              className="absolute right-3 top-1/2 -translate-y-1/2 text-grey-500 hover:text-white transition-colors"
            >
              <X size={14} />
            </button>
          )}
        </div>

        {notice && (
          <div className="flex items-center justify-between gap-3 px-4 py-2.5 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-xs text-emerald-300">
            <span className="flex items-center gap-2"><CheckCircle2 size={13} /> {notice}</span>
            <button onClick={() => setNotice(null)} aria-label="Dismiss"><X size={13} /></button>
          </div>
        )}
        {error && (
          <div className="flex items-center justify-between gap-3 px-4 py-2.5 rounded-xl bg-rose-500/15 border border-rose-500/30 text-xs text-rose-300">
            <span>{error}</span>
            <button onClick={() => setError(null)} aria-label="Dismiss"><X size={13} /></button>
          </div>
        )}

        <div className="flex items-center justify-between gap-3 flex-wrap">
          <SortBar sort={sort} direction={direction} onSort={onSort} />
          {data && data.users.length > 0 && (
            <label className="flex items-center gap-2 px-1 text-[11px] text-grey-400 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={allSelected}
                ref={(el) => {
                  // Indeterminate is a DOM property, not an attribute — React
                  // can't set it via JSX, so it has to be poked onto the node.
                  if (el) el.indeterminate = someSelected && !allSelected;
                }}
                onChange={toggleSelectAll}
                aria-label="Select all accounts on this page"
                className="w-3.5 h-3.5 rounded border-grey-600 bg-ink-950 accent-brand-400"
              />
              Select page
            </label>
          )}
        </div>

        {/* Only suspend/unsuspend in bulk — never delete. Deleting one account
            here demands typing its email precisely because it's irreversible;
            a checkbox that erases twenty would walk straight through that. */}
        {someSelected && (
          <div
            role="region"
            aria-label="Bulk actions"
            className="flex items-center justify-between gap-3 flex-wrap px-4 py-2.5 rounded-xl border border-brand-400/30 bg-brand-400/[0.07]"
          >
            <span className="text-xs text-brand-100">
              {pluralize([...selected].length, "account")} selected
            </span>
            <div className="flex items-center gap-2">
              <button
                onClick={() => bulkSuspend(true)}
                disabled={bulkBusy}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-rose-500/85 text-white hover:bg-rose-500 disabled:opacity-40 transition-colors"
              >
                {bulkBusy ? <Loader2 size={12} className="animate-spin" /> : <Ban size={12} />}
                Suspend
              </button>
              <button
                onClick={() => bulkSuspend(false)}
                disabled={bulkBusy}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-white/10 hover:bg-white/15 border border-white/20 text-white disabled:opacity-40 transition-colors"
              >
                <CheckCircle2 size={12} /> Unsuspend
              </button>
              <button
                onClick={() => setSelected(new Set())}
                disabled={bulkBusy}
                className="px-2.5 py-1.5 rounded-lg btn-ghost text-xs text-grey-300 disabled:opacity-40"
              >
                Clear
              </button>
            </div>
          </div>
        )}

        <div className="rounded-2xl border border-grey-700 bg-white/[0.03] overflow-hidden">
          {!data ? (
            <div aria-busy="true" aria-label="Loading users">
              {Array.from({ length: 6 }).map((_, i) => <RowSkeleton key={i} />)}
            </div>
          ) : data.users.length === 0 ? (
            <div className="px-4 py-16 text-center text-xs text-grey-500">
              {debounced ? `Nobody matches “${debounced}”.` : "No accounts yet."}
            </div>
          ) : (
            <div className={loading ? "opacity-60 transition-opacity" : "transition-opacity"}>
              {data.users.map((u) => {
                const open = openId === u.id;
                const selectable = !u.is_admin;
                return (
                  <div key={u.id} className="border-b border-grey-800 last:border-b-0">
                   <div className="flex items-stretch">
                    {/* Outside the button, not inside it: a checkbox nested in a
                        button is invalid HTML, and clicking it would toggle the
                        row open as well as tick the box. */}
                    <label className={`flex items-center pl-4 pr-1 shrink-0 ${
                      selectable ? "cursor-pointer" : "cursor-not-allowed"}`}>
                      <input
                        type="checkbox"
                        checked={selected.has(u.id)}
                        disabled={!selectable}
                        onChange={() => toggleSelect(u.id)}
                        aria-label={selectable
                          ? `Select ${u.email || u.name || u.id}`
                          : `${u.email || u.name || u.id} is an admin and can't be selected`}
                        className="w-3.5 h-3.5 rounded border-grey-600 bg-ink-950 accent-brand-400 disabled:opacity-25"
                      />
                    </label>
                    <button
                      onClick={() => setOpenId(open ? null : u.id)}
                      aria-expanded={open}
                      className="flex-1 min-w-0 text-left pl-1 pr-4 py-3 flex items-center gap-3 hover:bg-white/[0.03] transition-colors"
                    >
                      {u.avatar_url ? (
                        <img src={u.avatar_url} alt="" className="w-8 h-8 rounded-lg shrink-0" />
                      ) : (
                        <span className="w-8 h-8 shrink-0 rounded-lg bg-white/[0.06] border border-grey-700 flex items-center justify-center text-[11px] font-bold text-grey-300">
                          {(u.name || u.email || "?").trim().charAt(0).toUpperCase()}
                        </span>
                      )}

                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <span className="text-[13px] font-medium text-white truncate">
                            {u.name || u.email || u.id}
                          </span>
                          {u.is_admin && <Badge tone="amber">Admin</Badge>}
                          {u.suspended && <Badge tone="rose">Suspended</Badge>}
                          {u.plan === "pro" && <Badge tone="brand">Pro</Badge>}
                        </div>
                        <div className="text-[11px] text-grey-500 truncate flex items-center gap-1.5 mt-0.5">
                          {u.email_verified
                            ? <Mail size={10} className="shrink-0" />
                            : <MailX size={10} className="shrink-0 text-amber-500" />}
                          {u.email || "—"}
                          {u.github_login && (
                            <>
                              <Github size={10} className="shrink-0 ml-1" />
                              {u.github_login}
                            </>
                          )}
                        </div>
                      </div>

                      <div className="hidden sm:block text-right shrink-0">
                        <div className="text-[11px] text-grey-400 tabular-nums">
                          {u.generations} gen · {u.scans} scan
                        </div>
                        <div className="text-[10px] text-grey-600">
                          Joined {formatDate(u.created_at)}
                        </div>
                      </div>
                    </button>
                   </div>

                    {open && (
                      <UserPanel
                        user={u}
                        onChanged={patchUser}
                        onDeleted={(msg) => removeUser(u.id, msg)}
                        onError={setError}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {data && pages > 1 && (
          <div className="flex items-center justify-between gap-3">
            <span className="text-[11px] text-grey-500">Page {page} of {pages}</span>
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
                disabled={offset === 0}
                className="px-2.5 py-1.5 rounded-lg btn-ghost text-xs disabled:opacity-40 flex items-center gap-1"
              >
                <ChevronLeft size={12} /> Prev
              </button>
              <button
                onClick={() => setOffset((o) => o + PAGE_SIZE)}
                disabled={page >= pages}
                className="px-2.5 py-1.5 rounded-lg btn-ghost text-xs disabled:opacity-40 flex items-center gap-1"
              >
                Next <ChevronRight size={12} />
              </button>
            </div>
          </div>
        )}
      </div>
    </Page>
  );
}
