from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = PROJECT_ROOT / "static" / "index.html"


def test_primary_module_labels_use_new_names():
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert 'data-module="send-management">消息群发<' in source
    assert 'data-module="contact-management">分析师评分<' in source
    assert '高评分分析师' in source
    assert '消息群发默认启用“敬”' in source



def test_watch_engine_tables_use_signal_columns_and_summary_fallbacks():
    source = INDEX_HTML.read_text(encoding='utf-8')
    assert '<th class="read">来源</th>' in source
    assert '<th class="share">热度</th>' in source
    assert '<th class="recommend">新鲜度</th>' in source
    assert '<th class="transcribe">信号</th>' in source
    assert '<th class="content-type">议题</th>' in source
    assert '<th class="tone">信号</th>' in source
    assert 'function _deriveBriefFromItem' in source
    assert 'function _formatFreshness' in source
    assert '暂无摘要，可打开原文查看。' in source
    assert '暂无要点，可点击原文查看。' in source


def test_ai_summary_loads_config_before_local_generation():
    source = INDEX_HTML.read_text(encoding='utf-8')
    block = source.split('if (!contactRatings || Object.keys(contactRatings).length === 0)', 1)[1].split('const prompts = getSummaryPrompts', 1)[0]
    assert 'await loadAiConfig();' in block
    assert '生成摘要前加载 AI 配置失败' in block


def test_lazy_cached_lists_stay_visible_during_background_refresh():
    source = INDEX_HTML.read_text(encoding='utf-8')
    assert 'function renderLoadingIfNoCached' in source
    assert "显示上次内容，后台更新中…" in source
    assert "显示上次消息，后台更新中..." in source
    folo_block = source.split('async function refreshFolo()', 1)[1].split('async function refreshMpAgg()', 1)[0]
    assert "const cachedShown = showCachedList('folo-agg'" in folo_block
    assert 'renderLoadingIfNoCached(tbody, cachedShown, 11)' in folo_block
    assert 'if (!cachedShown) tbody.innerHTML' in folo_block
    mp_block = source.split('async function refreshMpAgg()', 1)[1].split('// legacy render path retained unreachable for safety', 1)[0]
    assert "const cachedShown = showCachedList('mp-agg'" in mp_block
    assert 'renderLoadingIfNoCached(tbody, cachedShown, 9)' in mp_block
    minutes_block = source.split('async function loadMinutes(refresh=false)', 1)[1].split('function updateMinutesDisplay()', 1)[0]
    assert "const cachedShown = showCachedList('minutes-agg'" in minutes_block
    assert 'renderLoadingIfNoCached(tbody, cachedShown, 9)' in minutes_block
    messages_block = source.split('async function loadRecentMessagesFromBackend', 1)[1].split('const now = new Date();', 1)[0]
    assert "cachedShown = showCachedList('message-list'" in messages_block
    assert 'if (!cachedShown) showMessageTableLoading();' in messages_block


def test_deeppupil_chinese_brand_is_visually_bold():
    source = INDEX_HTML.read_text(encoding='utf-8')
    block = source.split('.brand-name {', 1)[1].split('}', 1)[0]
    assert 'font-weight: 1000;' in block
    assert 'font-size: 34px;' in block
    assert '-webkit-text-stroke: .45px #000;' in block
    assert 'transform: scaleX(1.04);' in block
