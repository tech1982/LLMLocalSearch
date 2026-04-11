"""
Streamlit web UI for semantic search across Telegram & Instagram.
"""
import streamlit as st
import sys
import os
import re
import subprocess
import warnings
import urllib.request
import html as html_mod
warnings.filterwarnings("ignore", message=".*torch.classes.*")
warnings.filterwarnings("ignore", message=".*urllib3.*OpenSSL.*LibreSSL.*")
sys.path.insert(0, os.path.dirname(__file__))

from search_engine import search, generate_answer, get_stats, list_channels, DEFAULT_RESULTS


def _load_channel_categories() -> tuple[dict[str, str], list[str]]:
    """Parse channels.txt to extract category groupings.

    Returns (cat_map, category_order) where:
      cat_map = {channel_title: category}
      category_order = ordered list of category names from channels.txt
    """
    channels_file = os.path.join(os.path.dirname(__file__), "..", "channels.txt")
    if not os.path.exists(channels_file):
        return {}, []

    # Read category headers and channel usernames
    category = "Інше"
    category_order = []
    username_to_cat = {}
    with open(channels_file, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            # Detect category headers: "# --- Category Name ---"
            if stripped.startswith("#") and "---" in stripped:
                cat = stripped.strip("# -").strip()
                if cat:
                    category = cat
                    if cat not in category_order:
                        category_order.append(cat)
                continue
            # Skip comments / empty
            content = stripped.split("#")[0].strip()
            if not content:
                continue
            username = content.split("|")[0].strip()
            username_to_cat[username] = category

    # Map indexed channel titles to categories using channel_username from DB
    # list_channels() returns name=channel_title; we need username->title mapping
    # So we query DB for username mapping
    try:
        from search_engine import get_table
        tbl = get_table()
        if tbl.count_rows() > 0:
            arrow = tbl.to_lance().to_table(columns=["channel", "channel_username"])
            df = arrow.to_pandas().drop_duplicates()
            for _, row in df.iterrows():
                uname = row["channel_username"]
                title = row["channel"]
                if uname in username_to_cat:
                    username_to_cat[title] = username_to_cat.get(uname, "Інше")
    except Exception:
        pass

    # Ensure "Інше" is last if present
    if "Інше" not in category_order:
        category_order.append("Інше")

    return username_to_cat, category_order

st.set_page_config(
    page_title="🔍 Семантичний пошук",
    page_icon="🔍",
    layout="wide"
)

# --- Custom CSS: wider sidebar, modern chip styling ---
st.markdown("""
<style>
/* Wider sidebar */
[data-testid="stSidebar"] { min-width: 340px; max-width: 420px; }

/* Modern multiselect chips — muted blue-grey instead of red */
span[data-baseweb="tag"] {
    background-color: #e8eef4 !important;
    color: #1a3a5c !important;
    border: 1px solid #b8cce0 !important;
    border-radius: 6px !important;
}
span[data-baseweb="tag"] span[role="presentation"] {
    color: #5a7a96 !important;
}

/* Category headers in sidebar */
.channel-category {
    font-size: 0.75rem;
    font-weight: 600;
    color: #7f8c8d;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin: 0.6rem 0 0.2rem 0;
    padding: 0;
}
</style>
""", unsafe_allow_html=True)

st.title("🔍 Семантичний пошук")
st.caption("Telegram · Instagram — пошук за змістом і контекстом, а не лише за ключовими словами")

# Sidebar with stats and settings
with st.sidebar:
    st.header("⚙️ Налаштування")

    source = st.selectbox(
        "Джерело",
        ["all", "telegram", "instagram"],
        format_func=lambda x: {"all": "🌐 Всі джерела", "telegram": "📱 Telegram", "instagram": "📸 Instagram"}[x]
    )

    channels = list_channels()
    channel_names = [c["name"] for c in channels]
    channel_labels = {c["name"]: f"{c['name']} ({c['count']:,})" for c in channels}
    cat_map, category_order = _load_channel_categories()
    # Persist category order in session state so right panel can reorder it
    if "category_order" not in st.session_state:
        st.session_state["category_order"] = list(category_order)
    category_order = st.session_state["category_order"]

    if channel_names:
        # Initialize selected channels
        if "selected_channels" not in st.session_state:
            st.session_state["selected_channels"] = list(channel_names)

        col_a, col_b = st.columns(2)
        if col_a.button("✅ Всі", use_container_width=True):
            st.session_state["selected_channels"] = list(channel_names)
            st.rerun()
        if col_b.button("🗑️ Очистити", use_container_width=True):
            st.session_state["selected_channels"] = []
            st.rerun()

        # Group channels by category from channels.txt (preserve file order)
        from collections import OrderedDict
        grouped = OrderedDict()
        # Pre-seed with category order from channels.txt
        for cat in category_order:
            grouped[cat] = []
        for c in channels:
            cat = cat_map.get(c["name"], "Інше")
            grouped.setdefault(cat, []).append(c["name"])

        # Render grouped checkboxes
        for cat, cat_channels in grouped.items():
            if not cat_channels:
                continue
            current = st.session_state["selected_channels"]
            all_in = all(ch in current for ch in cat_channels)
            none_in = not any(ch in current for ch in cat_channels)

            # Category header with toggle
            cols = st.columns([4, 1])
            cols[0].markdown(f'<div class="channel-category">{cat}</div>', unsafe_allow_html=True)
            toggle_label = "−" if all_in else "+"
            if cols[1].button(toggle_label, key=f"cat_{cat}", use_container_width=True):
                if all_in:
                    st.session_state["selected_channels"] = [ch for ch in current if ch not in cat_channels]
                else:
                    st.session_state["selected_channels"] = list(set(current) | set(cat_channels))
                st.rerun()

            for ch in cat_channels:
                label = channel_labels.get(ch, ch)
                checked = ch in st.session_state["selected_channels"]
                new_val = st.checkbox(label, value=checked, key=f"ch_{ch}")
                if new_val and ch not in st.session_state["selected_channels"]:
                    st.session_state["selected_channels"].append(ch)
                elif not new_val and ch in st.session_state["selected_channels"]:
                    st.session_state["selected_channels"].remove(ch)

        selected_channels = st.session_state["selected_channels"]
    else:
        selected_channels = []
        st.info("Канали ще не проіндексовані. Запустіть скрипт індексації.")

    n_results = st.slider("Кількість результатів", 3, 50, DEFAULT_RESULTS)

    use_llm = st.toggle("🤖 Генерувати відповідь (Azure OpenAI)", value=True)

    answer_lang = st.selectbox(
        "Мова відповіді",
        ["uk", "en", "ru", "pl"],
        index=0,
        format_func=lambda x: {"en": "🇬🇧 Англійська", "uk": "🇺🇦 Українська", "ru": "🇷🇺 Російська", "pl": "🇵🇱 Польська"}[x]
    )

    st.divider()
    st.header("📊 Статистика")
    stats = get_stats()
    col1, col2 = st.columns(2)
    col1.metric("📱 Telegram", stats["telegram"])
    col2.metric("📸 Instagram", stats["instagram"])
    st.metric("📄 Всього документів", stats["total"])

# ─── Helper: run ingestion subprocess ────────────────────────────
def _run_ingestion(container, cmd_args: list[str], label: str):
    """Run an ingestion script and stream output into the given container."""
    venv_python = os.path.join(os.path.dirname(__file__), "..", ".venv", "bin", "python")
    if not os.path.exists(venv_python):
        venv_python = sys.executable
    script = os.path.join(os.path.dirname(__file__), cmd_args[0])
    full_cmd = [venv_python, script] + cmd_args[1:]
    with container.status(label, expanded=True) as status:
        st.text("Запуск...")
        try:
            proc = subprocess.Popen(
                full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=os.path.join(os.path.dirname(__file__), ".."),
            )
            output_lines = []
            log_area = st.empty()
            for line in proc.stdout:
                output_lines.append(line.rstrip())
                log_area.code("\n".join(output_lines[-30:]), language="text")
            proc.wait()
            if proc.returncode == 0:
                status.update(label=f"✅ {label} — готово", state="complete")
            else:
                status.update(label=f"❌ {label} — помилка (код {proc.returncode})", state="error")
        except Exception as e:
            status.update(label=f"❌ {label} — помилка", state="error")
            st.error(str(e))


# ─── Main layout: search (left) + admin panel (right) ───────────
main_col, admin_col = st.columns([3, 1])

with main_col:
    query = st.text_input(
        "Задайте питання",
        placeholder="Наприклад: Де знайти оренду житла у Варшаві?",
        label_visibility="collapsed"
    )

with admin_col:
    with st.expander("➕ Додати канал", expanded=True):
        channels_file = os.path.join(os.path.dirname(__file__), "..", "channels.txt")
        new_username = st.text_input(
            "Username або ID",
            placeholder="напр. wilanowbl",
            key="new_channel_username",
        )

        # Auto-resolve channel title from Telegram
        resolved_title = ""
        if new_username.strip() and not new_username.strip().lstrip("-").isdigit():
            uname = new_username.strip().lstrip("@")
            try:
                url = f"https://t.me/s/{uname}"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    page = resp.read().decode("utf-8", errors="ignore")
                # Extract <meta property="og:title" content="...">
                import re as _re
                m = _re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', page)
                if m:
                    resolved_title = html_mod.unescape(m.group(1))
            except Exception:
                pass

        if resolved_title:
            st.caption(f"📢 {resolved_title}")

        new_category = st.selectbox(
            "Категорія",
            category_order if category_order else ["Інше"],
            key="new_channel_category",
        )
        new_retention = st.number_input(
            "Зберігати (днів)",
            min_value=0,
            value=0,
            step=30,
            help="0 = без обмежень, інакше зберігає лише N останніх днів",
            key="new_channel_retention",
        )
        if st.button("Додати", key="add_channel_btn", use_container_width=True):
            if new_username.strip():
                username = new_username.strip().lstrip("@")
                comment = resolved_title
                line = username
                if int(new_retention) > 0:
                    line += f" | {int(new_retention)}:*"
                if comment:
                    padding = max(1, 24 - len(line))
                    line += " " * padding + "# " + comment
                with open(channels_file, encoding="utf-8") as f:
                    lines = f.readlines()
                insert_idx = len(lines)
                current_cat = None
                last_cat_line = {}
                for i, l in enumerate(lines):
                    stripped = l.strip()
                    if stripped.startswith("#") and "---" in stripped:
                        cat = stripped.strip("# -").strip()
                        if cat:
                            current_cat = cat
                    elif stripped and not stripped.startswith("#"):
                        if current_cat:
                            last_cat_line[current_cat] = i
                if new_category in last_cat_line:
                    insert_idx = last_cat_line[new_category] + 1
                lines.insert(insert_idx, line + "\n")
                with open(channels_file, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                st.success(f"✅ `{username}` → {new_category}")
                if comment:
                    st.caption(f"({comment})")
                st.caption("Запустіть індексацію.")
            else:
                st.warning("Введіть username або ID.")

    with st.expander("🔄 Індексація", expanded=True):
        if st.button("📱 Telegram — нові", use_container_width=True, key="btn_tg_sync"):
            _run_ingestion(st, ["ingest_telegram.py"], "Telegram: нові повідомлення")
        if st.button("📱 Telegram — бекфіл", use_container_width=True, key="btn_tg_backfill"):
            _run_ingestion(st, ["ingest_telegram.py", "--backfill"], "Telegram: бекфіл")
        if st.button("📸 Instagram", use_container_width=True, key="btn_ig"):
            _run_ingestion(st, ["ingest_instagram.py"], "Instagram: індексація")

    with st.expander("📋 Порядок категорій", expanded=True):
        order = st.session_state["category_order"]
        for idx, cat in enumerate(order):
            c1, c2, c3 = st.columns([1, 4, 1])
            if c1.button("▲", key=f"up_{cat}", disabled=(idx == 0), use_container_width=True):
                order[idx], order[idx - 1] = order[idx - 1], order[idx]
                st.session_state["category_order"] = order
                st.rerun()
            c2.markdown(f"**{cat}**")
            if c3.button("▼", key=f"dn_{cat}", disabled=(idx == len(order) - 1), use_container_width=True):
                order[idx], order[idx + 1] = order[idx + 1], order[idx]
                st.session_state["category_order"] = order
                st.rerun()

# ─── Search results ──────────────────────────────────────────────
if query:
    if not selected_channels:
        main_col.warning("Будь ласка, оберіть хоча б один канал для пошуку.")
        st.stop()

    with main_col:
        with st.spinner("🔎 Шукаємо..."):
            results = search(
                query=query,
                n_results=n_results,
                source_filter=source if source != "all" else None,
                channel_filter=selected_channels,
            )

        if not results:
            st.warning("Нічого не знайдено. Спробуйте переформулювати запит або переконайтесь, що індексація була виконана.")
        else:
            # LLM Answer
            if use_llm:
                with st.spinner("🤖 Генеруємо відповідь через Azure OpenAI..."):
                    answer = generate_answer(query, results, language=answer_lang)
                # Build source metadata: url + short date
                source_meta = {
                    i: {"url": r.get("url"), "date": (r.get("date") or "")[:10]}
                    for i, r in enumerate(results, 1)
                }
                max_source = len(results)

                def _linkify_brackets(m):
                    """Turn [N] and [N, M, ...] into dated links."""
                    nums_str = m.group(1)
                    nums = [int(x) for x in re.split(r"[,\s]+", nums_str)
                            if x.strip().isdigit() and 1 <= int(x.strip()) <= max_source]
                    if not nums:
                        return m.group(0)
                    parts = []
                    for n in nums:
                        meta = source_meta.get(n, {})
                        url = meta.get("url")
                        date = meta.get("date", "")
                        label = date if date else str(n)
                        parts.append(f"[[{label}]]({url})" if url else f"[{label}]")
                    return " ".join(parts)

                # Match [N] and [N, M, ...] — avoid matching markdown image syntax ![...
                answer = re.sub(r"(?<!!)\[([\d,\s]+)\]", _linkify_brackets, answer)
                st.markdown("### 💡 Відповідь")
                st.markdown(answer)
                st.divider()

            # Raw results
            st.markdown(f"### 📄 Знайдено {len(results)} релевантних повідомлень")

            for i, r in enumerate(results, 1):
                source_emoji = "📱" if r["source"] == "telegram" else "📸"

                with st.expander(
                    f"#{i} {source_emoji} {r['channel']}"
                    + (f" → {r['topic']}" if r.get('topic') else "")
                    + f" | {r['date']}",
                    expanded=(i <= 3)
                ):
                    if r.get("author"):
                        st.caption(f"👤 {r['author']}")

                    st.markdown(r["text"])

                    if r.get("url"):
                        st.markdown(f"[🔗 Відкрити оригінал]({r['url']})")
