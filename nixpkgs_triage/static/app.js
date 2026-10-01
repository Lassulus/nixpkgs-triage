"use strict";

// The list state lives in the URL query (category, sort, filters), so views can be bookmarked and shared.
const STATUS_POLL_MS = 30000;
const OUTPUT_POLL_MS = 3000;
const COLUMNS = 11;

let meta = null; // /api/status: filter and sort definitions, sync state
let state = null; // {category, sort, filters}
let listGeneration = null;
let next = null; // cursor of the next page; null when the list is complete
let loading = null; // AbortController of the page request in flight
let firstPageLoaded = false;

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

async function api(path, signal) {
  const res = await fetch(path, { signal });
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

// Same rules as util.age / util.fmt_duration in the CLI.
function age(ts) {
  const days = Math.floor((Date.now() - Date.parse(ts)) / 86400000);
  if (days >= 365) return `${Math.floor(days / 365)}y`;
  if (days >= 30) return `${Math.floor(days / 30)}mo`;
  return `${days}d`;
}

function duration(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(s / 86400)}d`;
}

const since = (ts) => duration(Date.now() - Date.parse(ts));
const until = (ts) => duration(Date.parse(ts) - Date.now());

function stateFromUrl() {
  const q = new URLSearchParams(location.search);
  const filters = {};
  for (const f of meta.filters) {
    const v = q.get(f.key);
    filters[f.key] = f.options.includes(v) ? v : f.options[0];
  }
  const sort = meta.sorts.includes(q.get("sort")) ? q.get("sort") : meta.sorts[0];
  return { category: q.get("category") || "all", sort, filters };
}

function query(extra = {}) {
  const q = new URLSearchParams();
  if (state.category !== "all") q.set("category", state.category);
  if (state.sort !== meta.sorts[0]) q.set("sort", state.sort);
  for (const f of meta.filters) if (state.filters[f.key] !== f.options[0]) q.set(f.key, state.filters[f.key]);
  for (const [k, v] of Object.entries(extra)) q.set(k, v);
  return q;
}

function setState(change) {
  Object.assign(state, change);
  const q = query().toString();
  history.replaceState(null, "", q ? `?${q}` : location.pathname);
  renderToolbar();
  reloadList();
}

// toolbar and categories

function select(label, value, options, onchange, changed) {
  return el(
    "label",
    {},
    label,
    el(
      "select",
      { class: changed ? "changed" : null, onchange: (e) => onchange(e.target.value) },
      options.map((o) => el("option", { value: o, selected: o === value }, o)),
    ),
  );
}

function renderToolbar() {
  const bar = $("#toolbar");
  bar.replaceChildren(
    select("sort", state.sort, meta.sorts, (v) => setState({ sort: v }), false),
    ...meta.filters.map((f) =>
      select(
        f.label,
        state.filters[f.key],
        f.options,
        (v) => setState({ filters: { ...state.filters, [f.key]: v } }),
        state.filters[f.key] !== f.options[0],
      ),
    ),
  );
}

let categoryList = [];

function renderCategories() {
  $("#categories").replaceChildren(
    ...categoryList.map(([name, n]) =>
      el(
        "button",
        {
          class: [name === state.category ? "selected" : "", n === 0 ? "empty" : ""].join(" "),
          onclick: () => {
            window.scrollTo(0, 0);
            setState({ category: name });
          },
        },
        el("span", {}, name),
        el("span", { class: "n" }, n),
      ),
    ),
  );
  // Narrow screens hide the sidebar; the toolbar gets a category select instead.
  if (window.matchMedia("(max-width: 800px)").matches) {
    const names = categoryList.map(([name]) => name);
    $("#toolbar").prepend(select("category", state.category, names, (v) => setState({ category: v }), false));
  }
}

// PR list

function statusCell(short) {
  return el("td", { class: short ? `st-${short}` : null }, short);
}

function prRow(pr) {
  const tr = el(
    "tr",
    {
      class: ["pr", pr.draft ? "draft" : "", pr.ci_failing ? "failing" : ""].join(" "),
      "data-number": pr.number,
      onclick: (e) => {
        if (e.target.closest("a") || window.getSelection().toString()) return;
        toggleDetail(tr, pr.number);
      },
    },
    el("td", {}, el("a", { href: `https://github.com/NixOS/nixpkgs/pull/${pr.number}`, target: "_blank" }, `#${pr.number}`)),
    el("td", { title: pr.created_at }, age(pr.created_at)),
    el("td", {}, el("span", { class: "add" }, `+${pr.additions}`), "/", el("span", { class: "del" }, `-${pr.deletions}`)),
    el("td", { class: "ci" }, pr.ci),
    el("td", {}, pr.conflict ? "yes" : ""),
    el("td", {}, pr.draft ? "yes" : ""),
    el("td", {}, pr.mark),
    statusCell(pr.check),
    statusCell(pr.review),
    el("td", { class: "category dim" }, pr.category),
    el("td", { class: "title" }, pr.title, el("span", { class: "author" }, pr.author)),
  );
  return tr;
}

function reloadList() {
  if (loading) loading.abort();
  loading = null;
  next = null;
  firstPageLoaded = false;
  closeAllDetails();
  $("#prs tbody").replaceChildren();
  $("#reload").hidden = true;
  loadPage();
}

async function loadPage() {
  if (loading || (firstPageLoaded && !next)) return;
  const controller = new AbortController();
  loading = controller;
  $("#end").textContent = "loading…";
  try {
    const page = await api(`/api/prs?${query(next ? { after: next } : {})}`, controller.signal);
    if (loading !== controller) return;
    if (!firstPageLoaded) {
      listGeneration = page.generation;
      categoryList = page.categories;
      renderToolbar();
      renderCategories();
      $("#counts").textContent =
        `${page.matching} of ${page.total_open} open match the filters` +
        (state.category === "all" ? "" : `, ${page.count} in ${state.category}`);
    }
    firstPageLoaded = true;
    $("#prs tbody").append(...page.prs.map(prRow));
    next = page.next;
    const shown = $("#prs tbody").querySelectorAll("tr.pr").length;
    $("#end").textContent = page.count === 0 ? "no PRs match" : next ? `${shown} of ${page.count}` : `all ${page.count} shown`;
  } catch (e) {
    if (e.name === "AbortError") return;
    $("#end").textContent = `loading failed: ${e.message}`;
  } finally {
    if (loading === controller) loading = null;
  }
  // A tall screen may still show the sentinel; keep filling it.
  if (next && isNearEnd()) loadPage();
}

function isNearEnd() {
  return $("#end").getBoundingClientRect().top < window.innerHeight + 800;
}

// detail rows

const openDetails = new Map(); // PR number -> {row, timer}

function closeDetail(number) {
  const d = openDetails.get(number);
  if (!d) return;
  clearTimeout(d.timer);
  d.row.remove();
  d.prRow.classList.remove("open");
  openDetails.delete(number);
}

function closeAllDetails() {
  for (const number of [...openDetails.keys()]) closeDetail(number);
}

async function toggleDetail(tr, number) {
  if (openDetails.has(number)) {
    closeDetail(number);
    return;
  }
  const box = el("div", { class: "detail-box" }, "loading…");
  const row = el("tr", { class: "detail" }, el("td", { colspan: COLUMNS }, box));
  tr.after(row);
  tr.classList.add("open");
  const d = { row, prRow: tr, box, timer: null, tab: null, log: false };
  openDetails.set(number, d);
  try {
    d.pr = await api(`/api/pr/${number}`);
  } catch (e) {
    box.textContent = `loading failed: ${e.message}`;
    return;
  }
  // Start on the output that exists: the check, else the review, else the changed files.
  d.tab = d.pr.jobs.check ? "check" : d.pr.jobs.review ? "review" : "files";
  renderDetail(number);
}

const JOB_TITLES = { check: "guideline check", review: "nixpkgs-review" };

function renderDetail(number) {
  const d = openDetails.get(number);
  if (!d) return;
  const pr = d.pr;
  const list = (xs) => (xs.length ? xs.join(", ") : "-");
  const info = el(
    "dl",
    {},
    el("dt", {}, "PR"),
    el("dd", {}, el("a", { href: pr.url, target: "_blank" }, pr.url), ` ${pr.title}`),
    el("dt", {}, "state"),
    el(
      "dd",
      {},
      `${pr.state.toLowerCase()}${pr.draft ? " draft" : ""} → ${pr.base_ref}, by ${pr.author} ` +
        `(${(pr.author_association || "").toLowerCase()}), opened ${age(pr.created_at)} ago, ` +
        `updated ${since(pr.updated_at)} ago`,
    ),
    el("dt", {}, "category"),
    el("dd", {}, `${pr.category} · tags: ${list(pr.tags)} · topics: ${list(pr.topics)}`),
    el("dt", {}, "size"),
    el(
      "dd",
      {},
      el("span", { class: "add" }, `+${pr.additions}`),
      " ",
      el("span", { class: "del" }, `-${pr.deletions}`),
      ` in ${pr.files_total} files · CI: ${pr.ci} · comments: ${pr.comments} · mark: ${pr.mark || "-"}`,
    ),
    el("dt", {}, "labels"),
    el("dd", {}, list(pr.labels)),
  );
  const jobs = el(
    "div",
    { class: "jobs" },
    Object.entries(JOB_TITLES).map(([kind, title]) => {
      const job = pr.jobs[kind];
      return el("div", { class: job ? `st-${job.short}` : "dim" }, `${title}: `, job ? job.describe : "not run");
    }),
  );
  const tab = (name, label) =>
    el(
      "button",
      {
        class: d.tab === name ? "selected" : null,
        onclick: () => {
          d.tab = name;
          d.log = false;
          renderDetail(number);
        },
      },
      label,
    );
  const tabs = el(
    "div",
    { class: "tabs" },
    tab("check", "guideline check"),
    tab("review", "nixpkgs-review"),
    tab("files", `files (${pr.files_total})`),
    el("span", { class: "spacer" }),
    d.tab !== "files" && pr.jobs[d.tab]
      ? el(
          "button",
          {
            onclick: () => {
              d.log = !d.log;
              renderDetail(number);
            },
          },
          d.log ? "show report" : "show log",
        )
      : null,
  );
  const pane = el("div", {});
  d.box.replaceChildren(info, jobs, tabs, pane);
  clearTimeout(d.timer);
  if (d.tab === "files") pane.append(filesTable(pr));
  else loadOutput(number, pane);
}

function filesTable(pr) {
  if (!pr.files.length) return el("p", { class: "dim" }, "no files");
  const counted = pr.files[0].additions !== null;
  const max = Math.max(1, ...pr.files.map((f) => (f.additions || 0) + (f.deletions || 0)));
  const rows = pr.files.map((f) =>
    el(
      "tr",
      {},
      el("td", { class: "num add" }, counted ? `+${f.additions}` : ""),
      el("td", { class: "num del" }, counted ? `-${f.deletions}` : ""),
      el(
        "td",
        {},
        counted
          ? el(
              "span",
              { class: "bar" },
              el("span", { class: "a", style: `width:${(100 * f.additions) / max}%` }),
              el("span", { class: "d", style: `width:${(100 * f.deletions) / max}%` }),
            )
          : null,
      ),
      el("td", { class: "dim" }, f.change),
      el("td", { class: "path" }, f.path),
    ),
  );
  const notes = [];
  if (!counted) notes.push(el("p", { class: "dim" }, "per-file line counts arrive with the next sync of this PR"));
  if (pr.files_total > pr.files.length)
    notes.push(
      el("p", { class: "dim" }, `… ${pr.files_total - pr.files.length} more files (GitHub lists the first ${pr.files.length})`),
    );
  return el("div", {}, el("table", { class: "files" }, el("tbody", {}, rows)), notes);
}

async function loadOutput(number, pane) {
  const d = openDetails.get(number);
  const kind = d.tab;
  if (!d.pr.jobs[kind]) {
    pane.replaceChildren(el("p", { class: "dim" }, `${JOB_TITLES[kind]} has not run for this PR`));
    return;
  }
  let out;
  try {
    out = await api(`/api/pr/${number}/${kind}${d.log ? "?log=1" : ""}`);
  } catch (e) {
    pane.replaceChildren(el("p", {}, `loading failed: ${e.message}`));
    return;
  }
  if (openDetails.get(number) !== d || d.tab !== kind) return; // closed or switched meanwhile
  const old = pane.querySelector("pre");
  const atBottom = !old || old.scrollTop + old.clientHeight >= old.scrollHeight - 20;
  const pre = el("pre", { class: out.is_log ? "output log" : "output" }, out.text ?? "(no output yet)");
  pane.replaceChildren(el("p", { class: "dim" }, `${JOB_TITLES[kind]} ${out.what}`), pre);
  if (out.is_log && atBottom) pre.scrollTop = pre.scrollHeight; // follow logs unless scrolled up
  else if (old) pre.scrollTop = old.scrollTop;
  if (out.job.active) {
    d.timer = setTimeout(async () => {
      // The job state line and the report/log switch change when the job finishes.
      try {
        d.pr = await api(`/api/pr/${number}`);
      } catch {}
      if (d.pr.jobs[kind] && !d.pr.jobs[kind].active) renderDetail(number);
      else loadOutput(number, pane);
    }, OUTPUT_POLL_MS);
  }
}

// header: sync state and new data

function renderStatus(status) {
  const parts = [status.last_sync ? `last sync ${since(status.last_sync)} ago` : "never synced"];
  const sync = status.sync;
  if (sync && sync.running) parts.push(`syncing: ${sync.line || "starting"}`);
  else if (sync && sync.last_exit) parts.push(`last sync run failed (exit ${sync.last_exit})`);
  else if (sync && sync.next) parts.push(`next sync in ${until(sync.next)}`);
  $("#sync").textContent = parts.join(" · ");
  if (listGeneration !== null && status.generation !== listGeneration) {
    // Rebuild the list in place only when that cannot move what the reader is looking at.
    if (window.scrollY < 50 && openDetails.size === 0) reloadList();
    else $("#reload").hidden = false;
  }
}

async function pollStatus() {
  try {
    renderStatus(await api("/api/status"));
  } catch {}
  setTimeout(pollStatus, STATUS_POLL_MS);
}

async function main() {
  meta = await api("/api/status");
  state = stateFromUrl();
  renderToolbar();
  renderStatus(meta);
  $("#reload").addEventListener("click", () => {
    window.scrollTo(0, 0);
    reloadList();
  });
  new IntersectionObserver((entries) => entries.some((e) => e.isIntersecting) && loadPage(), {
    rootMargin: "800px",
  }).observe($("#end"));
  reloadList();
  setTimeout(pollStatus, STATUS_POLL_MS);
}

main();
