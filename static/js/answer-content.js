const labels = {开口: 'conclusion', 要点: 'point', 材料: 'material', 待确认: 'unknown',
  方案: 'hypothesis', 验证: 'plan', 方向: 'direction'};
const names = {conclusion: '回答建议', point: '要点', material: '材料原文', unknown: '待确认',
  hypothesis: '假设方案', plan: '验证计划', explanation: '', text: ''};
const sourceNames = {'context:resume': '简历', 'context:candidate_speech': '本人发言',
  'context:papers': '论文材料', 'context:jd': '岗位要求', 'context:enrichment': '外部参考'};
const kindNames = {candidate_material: '候选人材料', candidate_speech: '本人自述',
  reference: '参考资料', paper: '论文资料', job_description: '岗位要求', external: '外部参考'};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

export function draftDocument(raw, level, sources = []) {
  const blocks = [], directions = [];
  if (level === 'detail') {
    if (raw) blocks.push({kind: 'explanation', title: '', text: raw});
  } else {
    let previous, fence = '';
    for (const line of raw.split('\n')) {
      const marker = /^\s{0,3}(`{3,}|~{3,})/.exec(line);
      const match = fence ? null : /^【([^】]+)】(.*)$/.exec(line.trim());
      if (match && labels[match[1]]) {
        const kind = labels[match[1]], body = match[2].trim(), separator = body.indexOf(':::');
        const title = separator < 0 ? '' : body.slice(0, separator).trim();
        const text = separator < 0 ? body : body.slice(separator + 3).trim();
        if (kind === 'direction') {
          previous = {name: separator < 0 ? body : title, description: separator < 0 ? '' : text};
          directions.push(previous);
        } else {
          previous = {kind, title, text}; blocks.push(previous);
        }
      } else if (previous || line.trim()) {
        if (previous) {
          const field = 'name' in previous ? 'description' : 'text';
          previous[field] += '\n' + line;
        } else {
          previous = {kind: 'text', title: '', text: line}; blocks.push(previous);
        }
      }
      if (marker) {
        if (!fence) fence = marker[1];
        else if (marker[1][0] === fence[0] && marker[1].length >= fence.length) fence = '';
      }
    }
    if (raw.trim() && !blocks.length) blocks.push({kind: 'text', title: '', text: raw});
  }
  return {version: 1, level, blocks, directions, sources, findings: [], complete: false};
}

export function isAnswerDocument(value, level) {
  return value?.version === 1 && value.level === level && Array.isArray(value.blocks)
    && Array.isArray(value.directions) && Array.isArray(value.sources) && Array.isArray(value.findings);
}

function sourcePanel(sources) {
  const root = node('details', 'answer-sources');
  root.append(node('summary', '', '资料依据 · ' + sources.length));
  const rows = sources.map((source, index) => {
    const row = node('details', 'source-item'); row.dataset.sourceIndex = index;
    const locator = source.lines ? ` · L${source.lines.join('-')}` : '';
    const name = sourceNames[source.source] || source.source;
    row.append(node('summary', '', `${kindNames[source.kind] || '资料'} · ${name}${locator}`));
    if (source.heading) row.append(node('div', 'source-heading', source.heading));
    if (source.partial || source.excerpt_truncated) row.append(node('div', 'source-partial', '节选'));
    row.append(node('pre', 'source-excerpt', source.excerpt || ''));
    root.append(row);
    return row;
  });
  return {root, open(indices) {
    root.open = true;
    for (const index of indices) if (rows[index]) rows[index].open = true;
    const first = rows[indices[0]];
    if (first) {
      first.scrollIntoView({block: 'nearest'});
      first.querySelector('summary').focus({preventScroll: true});
    }
  }};
}

function linkCitations(body, citations, panel, compact) {
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) {
    if (!walker.currentNode.parentElement.closest('pre,code,a')) textNodes.push(walker.currentNode);
  }
  for (const textNode of textNodes) {
    const text = textNode.textContent, fragment = document.createDocumentFragment();
    let offset = 0, changed = false;
    for (const match of text.matchAll(/〔[^〕]*〕/g)) {
      const citation = citations?.find(item => item.raw === match[0]);
      if (!citation) continue;
      fragment.append(document.createTextNode(text.slice(offset, match.index)));
      if (compact) {
        const note = node('sup', 'citation-note', citation.valid ? '[材料]' : '[?]');
        note.title = citation.valid ? '材料来源' : '来源待核实'; fragment.append(note);
      } else if (citation.valid) {
        const button = node('button', 'citation-ref', `[${citation.source_indices[0] + 1}]`);
        button.type = 'button'; button.title = match[0];
        button.setAttribute('aria-label', '查看来源 ' + match[0]);
        button.onclick = () => panel.open(citation.source_indices);
        fragment.append(button);
      } else {
        const unresolved = node('span', 'citation-unresolved', match[0]);
        unresolved.title = '来源待核实'; fragment.append(unresolved);
      }
      offset = match.index + match[0].length; changed = true;
    }
    if (changed) {
      fragment.append(document.createTextNode(text.slice(offset))); textNode.replaceWith(fragment);
    }
  }
}

export function speechReviewLabel(status) {
  return ({
    queued:'等待后台复听',
    pending:'后台复听中',
    matched:'复听文字一致',
    corrected:'已按复听修正',
    uncertain:'复听有分歧，保留首版',
    late:'复听已中断，未自动修正',
    timed_out:'复听超时，保留首版',
    failed:'复听不可用，保留首版',
    busy:'复听繁忙，保留首版',
    audio_unavailable:'复听音频缺失，保留原题',
    audio_budget:'复听片段超出时限，保留原题',
    audio_weak:'复听音量过低，保留原题',
  })[status] || '';
}

export function speechReviewEvidence(review) {
  if (!review?.evidence?.models?.length) return null;
  const trace = node('details', 'candidate-context');
  trace.append(node('summary', '', '复听依据'),
    node('p', '', review.evidence.models.join('、') + ' · ' + review.elapsed_ms + ' ms'));
  for (const [model, key] of [['Turbo','turbo_text'], ['large-v3','large_text']]) {
    if (review.evidence[key]) trace.append(node('div', '', model), node('pre', '', review.evidence[key]));
  }
  return trace;
}

export function renderRevisions(container, revisions) {
  container.hidden = !revisions.length;
  if (!revisions.length) return;
  const list = node('details', 'revision-list');
  list.open = !!container.querySelector('.revision-list[open]');
  list.append(node('summary', '', `先前版本（${revisions.length}）`));
  revisions.forEach((revision, index) => {
    const section = node('section', 'revision-entry');
    section.append(node('div', 'revision-caption', `版本 ${index + 1} · 已被替换`),
      node('p', 'revision-question', revision.question));
    if (revision.answer) {
      section.append(node('div', 'revision-caption',
        revision.answer_status === 'complete' ? '当时的回答' : '当时已生成的片段'));
      const body = node('div', 'revision-answer');
      renderDocument(body, draftDocument(revision.answer, 'quick'));
      section.append(body);
    }
    const status = speechReviewLabel(revision.speech_review?.status);
    if (status) section.append(node('div', 'turn-review-status', status));
    const trace = speechReviewEvidence(revision.speech_review);
    if (trace) section.append(trace);
    list.append(section);
  });
  container.replaceChildren(list);
}

export function renderDocument(container, value, {compact = false} = {}) {
  const openSources = [...container.querySelectorAll('.source-item[open]')].map(item => item.dataset.sourceIndex);
  const sourceRootOpen = !!container.querySelector('.answer-sources[open]');
  const fragment = document.createDocumentFragment(), panel = sourcePanel(value.sources);
  for (const block of value.blocks) {
    const section = node('section', 'answer-block'); section.dataset.kind = block.kind;
    const label = node('div', 'answer-block-label');
    let name = names[block.kind] || '';
    if (block.kind === 'material') {
      const supported = block.support?.status === 'quote_matched';
      section.dataset.support = supported ? 'quote_matched' : 'unverified';
      const source = value.sources[block.support?.source_indices?.[0]];
      name = supported ? (compact ? '材料摘录' : ['candidate_material', 'candidate_speech'].includes(source?.kind) ? '材料原文' : '参考摘录') : '摘录待核实';
      if (supported) label.append(node('span', 'quote-match', '原文匹配'));
    }
    if (name) label.prepend(node('span', 'answer-kind', name));
    if (block.title && block.kind !== 'material') label.append(node('strong', '', block.title));
    if (label.childNodes.length) section.append(label);
    const body = node('div', block.kind === 'conclusion' ? 'opener block-content' : 'block-content');
    // Only the server's MarkdownIt renderer supplies HTML; streamed drafts stay text.
    if (block.html !== undefined) {
      body.classList.add('formatted'); body.innerHTML = block.html;
    }
    else body.textContent = block.text;
    linkCitations(body, block.citations, panel, compact);
    section.append(body);
    if (!compact && block.kind === 'material' && block.support?.source_indices?.length) {
      const button = node('button', 'material-source', '查看原文'); button.type = 'button';
      button.onclick = () => panel.open(block.support.source_indices); section.append(button);
    }
    fragment.append(section);
  }
  const sourceFindings = value.findings.filter(item => /^(citation_|material_)/.test(item.code));
  if (sourceFindings.length) fragment.append(node('div', 'answer-review', `${sourceFindings.length} 处来源待核实`));
  if (!compact && value.sources.length) fragment.append(panel.root);
  container.replaceChildren(fragment);
  panel.root.open = sourceRootOpen;
  for (const index of openSources) panel.root.querySelector(`[data-source-index="${index}"]`)?.setAttribute('open', '');
}
