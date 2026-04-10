"""
Streamlit web UI for semantic search across Telegram & Instagram.
"""
import streamlit as st
import sys
import os
import re
import warnings
warnings.filterwarnings("ignore", message=".*torch.classes.*")
sys.path.insert(0, os.path.dirname(__file__))

from search_engine import search, generate_answer, get_stats, list_channels


def _load_channel_categories() -> dict[str, str]:
    """Parse channels.txt to extract category groupings.

    Returns {channel_title: category} by mapping channel usernames to
    their indexed titles via list_channels() data.
    """
    channels_file = os.path.join(os.path.dirname(__file__), "..", "channels.txt")
    if not os.path.exists(channels_file):
        return {}

    # Read category headers and channel usernames
    category = "Інше"
    username_to_cat = {}
    with open(channels_file, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            # Detect category headers: "# --- Category Name ---"
            if stripped.startswith("#") and "---" in stripped:
                cat = stripped.strip("# -").strip()
                if cat:
                    category = cat
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

    return username_to_cat

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
    cat_map = _load_channel_categories()

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

        # Group channels by category from channels.txt
        from collections import OrderedDict
        grouped = OrderedDict()
        for c in channels:
            cat = cat_map.get(c["name"], "Інше")
            grouped.setdefault(cat, []).append(c["name"])

        # Render grouped checkboxes
        for cat, cat_channels in grouped.items():
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

    n_results = st.slider("Кількість результатів", 3, 30, 20)

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

    st.divider()
    st.caption("Нові повідомлення (інкрементально):")
    st.code("source .venv/bin/activate\npython src/ingest_telegram.py", language="bash")
    st.caption("Старі повідомлення (бекфіл, яких не вистачає):")
    st.code("source .venv/bin/activate\npython src/ingest_telegram.py --backfill", language="bash")
    st.caption("Instagram:")
    st.code("source .venv/bin/activate\npython src/ingest_instagram.py", language="bash")

# Main search interface
query = st.text_input(
    "Задайте питання",
    placeholder="Наприклад: Де знайти оренду житла у Варшаві?",
    label_visibility="collapsed"
)

if query:
    if not selected_channels:
        st.warning("Будь ласка, оберіть хоча б один канал для пошуку.")
        st.stop()

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
            # Replace [Source N] with clickable markdown links
            source_urls = {i: r.get("url") for i, r in enumerate(results[:12], 1)}
            def _linkify_source(m):
                n = int(m.group(1))
                url = source_urls.get(n)
                return f"[[Source {n}]]({url})" if url else m.group(0)
            answer = re.sub(r"\[Source (\d+)\]", _linkify_source, answer)
            st.markdown("### 💡 Відповідь")
            st.markdown(answer)
            st.divider()

        # Raw results
        st.markdown(f"### 📄 Знайдено {len(results)} релевантних повідомлень")

        for i, r in enumerate(results, 1):
            source_emoji = "📱" if r["source"] == "telegram" else "📸"
            similarity_pct = f"{r['similarity']*100:.1f}%"

            with st.expander(
                f"{source_emoji} {r['channel']}"
                + (f" → {r['topic']}" if r.get('topic') else "")
                + f" | {r['date']} | 🎯 {similarity_pct}",
                expanded=(i <= 3)
            ):
                if r.get("author"):
                    st.caption(f"👤 {r['author']}")

                st.markdown(r["text"])

                if r.get("url"):
                    st.markdown(f"[🔗 Відкрити оригінал]({r['url']})")
