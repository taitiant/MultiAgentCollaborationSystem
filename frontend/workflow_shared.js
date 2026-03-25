(function () {
  const DEFAULT_STAGES = [
    { name: 'requirements', stage_type: 'requirements', label: '需求分析' },
    { name: 'architecture', stage_type: 'architecture', label: '架构设计' },
    { name: 'coding', stage_type: 'coding', label: '编码实现' },
    { name: 'testing', stage_type: 'testing', label: '测试验证' },
    { name: 'docs', stage_type: 'docs', label: '文档产出' },
  ];

  const STAGE_TYPE_ALIASES = {
    requirements: 'requirements',
    requirement: 'requirements',
    analysis: 'requirements',
    clarification: 'requirements',
    architecture: 'architecture',
    arch: 'architecture',
    design: 'architecture',
    solution: 'architecture',
    coding: 'coding',
    code: 'coding',
    implementation: 'coding',
    develop: 'coding',
    build: 'coding',
    bugfix: 'coding',
    fix: 'coding',
    testing: 'testing',
    test: 'testing',
    qa: 'testing',
    verification: 'testing',
    validation: 'testing',
    docs: 'docs',
    doc: 'docs',
    documentation: 'docs',
    readme: 'docs',
  };

  const STAGE_META = {
    requirements: { label: '需求分析', icon: '📝' },
    architecture: { label: '架构设计', icon: '🏗️' },
    coding: { label: '编码实现', icon: '💻' },
    testing: { label: '测试验证', icon: '🧪' },
    docs: { label: '文档产出', icon: '📘' },
    created: { label: '任务创建', icon: '🟦' },
    'graph-run': { label: '流程收敛', icon: '✅' },
    'dev-loop': { label: '开发闭环', icon: '♻️' },
  };

  const NODE_STATUS_LABELS = {
    pending: '待执行',
    running: '执行中',
    done: '已完成',
    rework: '返工中',
    error: '异常',
    waiting: '待处理',
  };
  const NODE_MIN_WIDTH = 162;
  const NODE_MAX_WIDTH = 220;
  const NODE_MIN_HEIGHT = 108;
  const RUNTIME_THRESHOLDS = {
    requirements: { warn: 45, critical: 120 },
    architecture: { warn: 45, critical: 120 },
    coding: { warn: 90, critical: 240 },
    testing: { warn: 60, critical: 180 },
    docs: { warn: 45, critical: 120 },
    default: { warn: 60, critical: 180 },
  };

  function normalizeStageType(stageType) {
    const raw = String(stageType || '').trim().toLowerCase();
    if (!raw) return 'requirements';
    return STAGE_TYPE_ALIASES[raw] || raw;
  }

  function nodeStatusLabel(status) {
    return NODE_STATUS_LABELS[String(status || 'pending')] || String(status || 'pending');
  }

  function sanitizeNodeText(value) {
    return String(value || '')
      .split(/\r?\n+/)
      .map((line) => line.trim())
      .filter(Boolean)
      .filter((line) => !/^依赖[:：\s]/.test(line) && !/^depends?\b/i.test(line));
  }

  function compactNodeText(value, fallback = '') {
    const lines = sanitizeNodeText(value);
    return lines.length ? lines.join(' / ') : fallback;
  }

  function primaryNodeText(value, fallback = '') {
    const lines = sanitizeNodeText(value);
    return lines[0] || fallback;
  }

  function stageProcessLabel(statusKind, message) {
    const payload = message?.payload || {};
    const map = {
      agent_waiting: '等待模型返回',
      agent_returned: '整理模型结果',
      review_started: '正在发起评审',
      review_rework: '按评审返工中',
      smoke_rework: '自动修复中',
      prerequisite_rework: '前置返工处理中',
      agent_error: '模型调用失败',
    };
    if (String(statusKind || '') === 'review_finished') {
      if (payload?.review?.review_status === 'skipped') return '已跳过评审';
      if (payload?.review?.pass === true) return '评审通过';
      if (payload?.review?.pass === false) return '评审未通过';
      return '评审已完成';
    }
    return map[String(statusKind || '')] || '';
  }

  function basename(path) {
    const raw = String(path || '').trim().replace(/\\/g, '/');
    if (!raw) return '';
    const parts = raw.split('/');
    return parts[parts.length - 1] || raw;
  }

  function runtimeThreshold(stageType) {
    return RUNTIME_THRESHOLDS[normalizeStageType(stageType)] || RUNTIME_THRESHOLDS.default;
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds || 0)));
    if (!total) return '0s';
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours > 0) return `${hours}h ${minutes}m`;
    if (minutes > 0) return `${minutes}m ${secs}s`;
    return `${secs}s`;
  }

  function stageDisplayStatus(itemState) {
    if (itemState?.lastReviewPass === true) return '评审通过';
    if (itemState?.lastReviewPass === false) return '评审未过';
    return nodeStatusLabel(itemState?.status || 'pending');
  }

  function normalizeStageDefinitions(stageDefinitions) {
    const source = Array.isArray(stageDefinitions) && stageDefinitions.length ? stageDefinitions : DEFAULT_STAGES;
    return source.map((stage, index) => {
      const name = String(stage.name || stage.key || stage.id || `${normalizeStageType(stage.stage_type || 'requirements')}_${index + 1}`);
      const stageType = normalizeStageType(stage.stage_type || name);
      return {
        name,
        key: name,
        stage_type: stageType,
        label: String(stage.label || STAGE_META[stageType]?.label || name),
        role: String(stage.role || ''),
        capabilities: Array.isArray(stage.capabilities) ? stage.capabilities : [],
        depends_on: Array.isArray(stage.depends_on) ? stage.depends_on : [],
        human_checkpoint: Boolean(stage.human_checkpoint),
        index,
      };
    });
  }

  function isStageEvent(stageDefinitions, eventKey) {
    return normalizeStageDefinitions(stageDefinitions).some((stage) => stage.name === eventKey);
  }

  function stageIcon(stageTypeOrKey) {
    const raw = String(stageTypeOrKey || '');
    if (STAGE_META[raw]) return STAGE_META[raw].icon;
    const prefix = raw.includes(':') ? raw.split(':', 1)[0] : raw;
    if (STAGE_META[prefix]) return STAGE_META[prefix].icon;
    const normalized = normalizeStageType(raw);
    if (STAGE_META[normalized]) return STAGE_META[normalized].icon;
    const normalizedPrefix = normalizeStageType(prefix);
    return STAGE_META[normalizedPrefix]?.icon || '•';
  }

  function summarizeStages({ stageDefinitions, events, isExecuting }) {
    const stages = normalizeStageDefinitions(stageDefinitions);
    const stageState = {};
    stages.forEach((stage) => {
      stageState[stage.name] = {
        stageType: stage.stage_type,
        label: stage.label,
        status: 'pending',
        logs: [],
        attempts: 0,
        lastReason: '',
        lastEventType: '',
        lastReviewPass: null,
        lastFeedback: '',
        lastTimestamp: 0,
        runningSince: 0,
        lastProgressAt: 0,
        currentFile: '',
        currentIndex: 0,
        completedFiles: 0,
        totalFiles: 0,
        durationText: '',
        runtimeLevel: 'normal',
        runtimeAlert: '',
        progressText: '',
        progressMessage: '',
        rerunCount: 0,
        rerunStatus: '',
        rerunRequestedAt: 0,
        rerunCompletedAt: 0,
        rerunStageLabel: '',
      };
    });

    let lastStartedStage = '';
    (events || []).forEach((event) => {
      const stageName = event?.payload?.stage;
      if (!stageName || !stageState[stageName]) return;
      const item = stageState[stageName];
      const payload = event?.payload || {};
      const ts = Number(event.timestamp || 0);
      item.logs.push(event.event_type);
      item.lastEventType = event.event_type;
      item.lastTimestamp = ts;
      if (payload.reason) item.lastReason = String(payload.reason);
      if (payload.feedback || payload.error) item.lastFeedback = String(payload.feedback || payload.error || '');
      if (payload.label) item.rerunStageLabel = String(payload.label);
      if (event.event_type === 'StageRerunRequested') {
        item.rerunCount += 1;
        item.rerunStatus = 'requested';
        item.rerunRequestedAt = ts || item.rerunRequestedAt;
        item.status = 'running';
        item.runningSince = ts || item.runningSince;
        item.lastProgressAt = ts || item.lastProgressAt;
        item.progressMessage = '人工触发重跑';
      }
      if (event.event_type === 'StageStart') {
        item.status = 'running';
        item.attempts += 1;
        item.runningSince = ts || item.runningSince;
        item.lastProgressAt = ts || item.lastProgressAt;
        item.currentFile = '';
        item.currentIndex = 0;
        item.completedFiles = 0;
        item.totalFiles = 0;
        item.progressMessage = payload.reason ? String(payload.reason) : '';
        lastStartedStage = stageName;
      }
      if (event.event_type === 'StageProgress') {
        item.status = 'running';
        item.runningSince = item.runningSince || ts;
        item.lastProgressAt = ts || item.lastProgressAt;
        if (payload.current_file) item.currentFile = String(payload.current_file);
        if (payload.current_index != null) item.currentIndex = Number(payload.current_index || 0);
        if (payload.progress_completed != null) item.completedFiles = Number(payload.progress_completed || 0);
        if (payload.progress_total != null) item.totalFiles = Number(payload.progress_total || 0);
        if (payload.message) item.progressMessage = String(payload.message);
      }
      if (event.event_type === 'StageDone') item.status = 'done';
      if (event.event_type === 'StageRerunRequested') {
        item.status = 'running';
        item.rerunStatus = 'requested';
        item.runningSince = ts || item.runningSince;
        item.lastProgressAt = ts || item.lastProgressAt;
        item.progressMessage = payload.message ? String(payload.message) : '已触发阶段重跑';
      }
      if (event.event_type === 'StageRerunDone') {
        item.status = 'done';
        item.rerunStatus = 'done';
        item.rerunCompletedAt = ts || item.rerunCompletedAt;
        item.currentFile = '';
        item.currentIndex = 0;
        item.progressMessage = '';
        item.runtimeAlert = '';
      }
      if (event.event_type === 'StageRerunFailed') {
        item.status = 'rework';
        item.rerunStatus = 'failed';
        item.rerunCompletedAt = ts || item.rerunCompletedAt;
        item.currentFile = '';
        item.currentIndex = 0;
      }
      if (event.event_type === 'StageRerunError') {
        item.status = 'error';
        item.rerunStatus = 'error';
        item.rerunCompletedAt = ts || item.rerunCompletedAt;
        item.currentFile = '';
        item.currentIndex = 0;
      }
      if (event.event_type === 'StageRerunAborted') {
        item.status = 'rework';
        item.rerunStatus = 'aborted';
        item.rerunCompletedAt = ts || item.rerunCompletedAt;
        item.currentFile = '';
        item.currentIndex = 0;
      }
      if (event.event_type === 'StageReview' && event?.payload?.pass === false) {
        item.status = 'rework';
        item.lastReviewPass = false;
      }
      if (event.event_type === 'StageReview' && event?.payload?.pass === true) {
        item.lastReviewPass = true;
        item.status = 'done';
        item.currentFile = '';
        item.currentIndex = 0;
        item.progressMessage = '';
        item.runtimeAlert = '';
      }
      if (event.event_type === 'StageRework') item.status = item.status === 'error' ? 'error' : 'rework';
      if (event.event_type === 'StageError') item.status = 'error';
      if (event.event_type === 'StageAbort') item.status = 'rework';
      if (event.event_type === 'StageAwait') item.status = 'waiting';
      if (['StageDone', 'StageError', 'StageAbort', 'StageAwait'].includes(event.event_type)) {
        item.currentFile = '';
        item.currentIndex = 0;
        item.progressMessage = '';
      }
    });

    if ((events || []).some((event) => event.event_type === 'GraphError') && lastStartedStage && stageState[lastStartedStage]?.status === 'running') {
      stageState[lastStartedStage].status = 'error';
    }
    if (isExecuting && !stages.some((stage) => stageState[stage.name]?.status === 'running')) {
      const firstPending = stages.find((stage) => stageState[stage.name]?.status === 'pending');
      if (firstPending) stageState[firstPending.name].status = 'running';
    }
    const nowTs = Date.now() / 1000;
    stages.forEach((stage) => {
      const item = stageState[stage.name];
      if (!item || item.status !== 'running') return;
      const startedAt = Number(item.runningSince || item.lastTimestamp || 0);
      const lastProgressAt = Number(item.lastProgressAt || startedAt || 0);
      const runningSeconds = startedAt ? Math.max(0, nowTs - startedAt) : 0;
      const idleSeconds = lastProgressAt ? Math.max(0, nowTs - lastProgressAt) : runningSeconds;
      const thresholds = runtimeThreshold(item.stageType);
      item.durationText = runningSeconds ? formatDuration(runningSeconds) : '';
      item.runtimeLevel = 'normal';
      item.runtimeAlert = '';
      if (Number(item.totalFiles || 0) > 0) {
        const completed = Math.min(Number(item.completedFiles || 0), Number(item.totalFiles || 0));
        item.progressText = `${completed}/${Number(item.totalFiles || 0)}`;
      } else {
        item.progressText = '';
      }
      if (idleSeconds >= thresholds.critical) {
        item.runtimeLevel = 'critical';
        item.runtimeAlert = `已 ${formatDuration(idleSeconds)} 无新推进，可能卡在模型或外部请求`;
      } else if (idleSeconds >= thresholds.warn) {
        item.runtimeLevel = 'warning';
        item.runtimeAlert = `已 ${formatDuration(idleSeconds)} 无新推进，执行偏慢`;
      }
    });
    return stageState;
  }

  function computeTaskInsights({ stageDefinitions, stageState, events, eventLabels, taskStatus }) {
    const stages = normalizeStageDefinitions(stageDefinitions);
    const doneCount = stages.filter((stage) => stageState[stage.name]?.status === 'done').length;
    const runningStageDef = stages.find((stage) => stageState[stage.name]?.status === 'running');
    const runningStage = runningStageDef?.label || '无';
    const runningItem = runningStageDef ? stageState[runningStageDef.name] : null;
    const reworkCount = (events || []).filter((event) => event.event_type === 'StageRework').length;
    const activeBlockers = new Set(
      stages
        .filter((stage) => ['error', 'rework', 'waiting'].includes(stageState[stage.name]?.status))
        .map((stage) => stage.name),
    );
    const lastError = activeBlockers.size
      ? [...(events || [])].reverse().find((event) => {
        const eventType = String(event?.event_type || '');
        const stageName = String(event?.payload?.stage || '');
        if (!activeBlockers.has(stageName)) return false;
        return ['StageError', 'StageReview', 'StageRework', 'StageAwait', 'StageRerunError', 'StageRerunFailed', 'StageRerunAborted'].includes(eventType);
      })
      : null;
    const lastProgress = [...(events || [])].reverse().find((event) => ['StageProgress', 'StageDone', 'StageStart', 'GraphRun', 'StageRerunDone'].includes(event.event_type));
    const runningFile = basename(runningItem?.currentFile || '');
    const runningProgress = runningItem?.progressText || '';
    const runningDuration = runningItem?.durationText || '';
    const runtimeWarning = runningItem?.runtimeAlert || '';
    const runtimeLevel = runningItem?.runtimeLevel || 'normal';
    let lastProgressText = '暂无';
    if (lastProgress) {
      const label = eventLabels?.[lastProgress.event_type] || lastProgress.event_type;
      const stageLabel = lastProgress.payload?.label || lastProgress.payload?.stage || '';
      const progressFile = basename(lastProgress.payload?.current_file || '');
      const progressTotal = Number(lastProgress.payload?.progress_total || 0);
      const progressDone = Number(lastProgress.payload?.progress_completed || 0);
      const progressSuffix = progressTotal ? ` ${progressDone}/${progressTotal}` : '';
      const progressDetail = progressFile || lastProgress.payload?.message || '';
      lastProgressText = [label, stageLabel, progressDetail ? `${progressDetail}${progressSuffix}`.trim() : ''].filter(Boolean).join(' · ');
    }
    if ((stages.length && doneCount === stages.length) || String(taskStatus || '') === 'completed') {
      lastProgressText = '任务完成 · 全部阶段已收敛';
    }
    return {
      stageCount: stages.length,
      doneCount,
      runningStage,
      reworkCount,
      blocker: lastError ? (lastError.payload?.feedback || lastError.payload?.error || lastError.event_type) : '无阻塞',
      lastProgress: lastProgressText,
      runningFile,
      runningProgress,
      runningDuration,
      runtimeWarning,
      runtimeLevel,
    };
  }

  function summarizeFailure(stageKey, events) {
    const evt = [...(events || [])].reverse().find((event) => event?.payload?.stage === stageKey && ['StageError', 'StageReview', 'StageRework'].includes(event.event_type));
    if (!evt) return '';
    return String(evt.payload?.feedback || evt.payload?.error || evt.payload?.reason || '').slice(0, 180);
  }

  function estimateNodeSize({ title, role, meta, submeta, phase }) {
    const titleLen = String(title || '').length;
    const roleLen = String(role || '').length;
    const metaLen = String(meta || '').length;
    const phaseLen = String(phase || '').length;
    const chipsLen = (submeta || []).map((item) => String(item?.text || item || '').length).reduce((sum, len) => sum + len, 0);
    const width = Math.max(
      NODE_MIN_WIDTH,
      Math.min(
        NODE_MAX_WIDTH,
        146 + Math.max(titleLen * 2.8, roleLen * 2.2, metaLen * 2.4, chipsLen * 1.3),
      ),
    );
    const titleLines = Math.min(2, Math.max(1, Math.ceil(titleLen / 14)));
    const roleLines = roleLen ? Math.min(2, Math.max(1, Math.ceil(roleLen / 18))) : 0;
    const metaLines = metaLen ? Math.min(2, Math.max(1, Math.ceil(metaLen / 16))) : 0;
    const phaseLines = phaseLen ? Math.min(2, Math.max(1, Math.ceil(phaseLen / 16))) : 0;
    const chipLines = (submeta || []).length > 1 && chipsLen > 18 ? 2 : ((submeta || []).length ? 1 : 0);
    const height = Math.max(NODE_MIN_HEIGHT, 38 + titleLines * 20 + roleLines * 18 + metaLines * 18 + phaseLines * 18 + chipLines * 24);
    return { width: Math.round(width), height: Math.round(height) };
  }

  function buildStageProcessMap(conversationMessages) {
    const processMap = {};
    (conversationMessages || []).forEach((message) => {
      if (String(message?.message_type || '') !== 'system_status') return;
      const stageName = String(message?.stage_name || '');
      if (!stageName) return;
      const createdAt = Number(message?.created_at || 0);
      if (!processMap[stageName] || createdAt >= Number(processMap[stageName].created_at || 0)) {
        const statusKind = String(message?.payload?.status_kind || '');
        processMap[stageName] = {
          created_at: createdAt,
          label: stageProcessLabel(statusKind, message) || primaryNodeText(message?.content || '', ''),
          tone: String(message?.payload?.status_level || 'active'),
          kind: statusKind,
        };
      }
    });
    return processMap;
  }

  function latestGraphEvent(events) {
    return [...(events || [])].reverse().find((event) => ['GraphRun', 'GraphError', 'GraphAbort'].includes(String(event?.event_type || ''))) || null;
  }

  function computeGraphNodeStatus({ events, taskStatus, isExecuting, stageState, stages }) {
    const normalizedTaskStatus = String(taskStatus || '');
    const statuses = (stages || []).map((stage) => String(stageState?.[stage.name]?.status || 'pending'));
    if (statuses.length && statuses.every((status) => status === 'done')) return 'done';
    if (normalizedTaskStatus === 'completed') return 'done';
    if (normalizedTaskStatus === 'aborted') return 'rework';
    if (isExecuting || normalizedTaskStatus === 'running' || statuses.includes('running')) return 'running';
    if (normalizedTaskStatus === 'waiting_user' || statuses.includes('waiting')) return 'waiting';
    if (statuses.includes('error')) return 'error';
    if (statuses.includes('rework')) return 'rework';
    const latest = latestGraphEvent(events);
    if (latest?.event_type === 'GraphRun') return 'done';
    if (latest?.event_type === 'GraphAbort') return 'rework';
    if (latest?.event_type === 'GraphError') return 'error';
    return 'pending';
  }

  function computeNodePath(fromNode, toNode) {
    const halfWidth = Number(fromNode.width || NODE_MIN_WIDTH) / 2;
    const halfHeight = Number(fromNode.height || NODE_MIN_HEIGHT) / 2;
    const targetHalfWidth = Number(toNode.width || NODE_MIN_WIDTH) / 2;
    const targetHalfHeight = Number(toNode.height || NODE_MIN_HEIGHT) / 2;
    const fromCx = fromNode.x + halfWidth;
    const fromCy = fromNode.y + halfHeight;
    const toCx = toNode.x + targetHalfWidth;
    const toCy = toNode.y + targetHalfHeight;
    const dx = toCx - fromCx;
    const dy = toCy - fromCy;
    const horizontal = Math.abs(dx) >= Math.abs(dy);
    const startX = horizontal ? fromCx + (dx >= 0 ? halfWidth : -halfWidth) : fromCx;
    const startY = horizontal ? fromCy : fromCy + (dy >= 0 ? halfHeight : -halfHeight);
    const endX = horizontal ? toCx - (dx >= 0 ? targetHalfWidth : -targetHalfWidth) : toCx;
    const endY = horizontal ? toCy : toCy - (dy >= 0 ? targetHalfHeight : -targetHalfHeight);
    const bend1X = horizontal ? startX + dx * 0.35 : startX;
    const bend1Y = horizontal ? startY : startY + dy * 0.35;
    const bend2X = horizontal ? endX - dx * 0.25 : endX;
    const bend2Y = horizontal ? endY - dy * 0.25 : endY;
    return `M ${startX} ${startY} C ${bend1X} ${bend1Y}, ${bend2X} ${bend2Y}, ${endX} ${endY}`;
  }

  function detectDevelopmentGroups(stages) {
    const groups = [];
    let current = null;
    stages.forEach((stage, index) => {
      const isDev = ['coding', 'testing'].includes(normalizeStageType(stage.stage_type));
      if (!isDev) {
        if (current && current.stages.length > 1) groups.push(finalizeGroup(current));
        current = null;
        return;
      }
      if (!current) current = { startIndex: index, stages: [] };
      current.stages.push(stage);
    });
    if (current && current.stages.length > 1) groups.push(finalizeGroup(current));
    return groups;
  }

  function finalizeGroup(group) {
    const stageKeys = group.stages.map((stage) => stage.name);
    return {
      ...group,
      endIndex: group.startIndex + group.stages.length - 1,
      stageKeys,
      id: `dev-loop:${stageKeys.join('|')}`,
      title: '开发闭环',
    };
  }

  function aggregateGroupStatus(stageKeys, stageState) {
    const states = stageKeys.map((key) => stageState[key]?.status || 'pending');
    if (states.includes('error')) return 'error';
    if (states.includes('rework')) return 'rework';
    if (states.includes('running')) return 'running';
    if (states.length && states.every((status) => status === 'done')) return 'done';
    if (states.includes('waiting')) return 'waiting';
    return 'pending';
  }

  function relatedCodingStage(stages, stageName) {
    const index = stages.findIndex((stage) => stage.name === stageName);
    if (index < 0) return null;
    for (let i = index - 1; i >= 0; i -= 1) {
      if (normalizeStageType(stages[i].stage_type) === 'coding') return stages[i];
    }
    return stages.find((stage) => normalizeStageType(stage.stage_type) === 'coding') || null;
  }

  function shouldShowSmokeLoop(stageName, events, stageState) {
    const item = stageState[stageName] || {};
    if (String(item.lastReason || '').toLowerCase().includes('smoke')) return true;
    return [...(events || [])].reverse().some((event) => event?.payload?.stage === stageName && String(event?.payload?.reason || '').toLowerCase().includes('smoke'));
  }

  function shouldShowTestingLoop(stageName, events, stageState) {
    const item = stageState[stageName] || {};
    const reason = String(item.lastReason || '').toLowerCase();
    if (reason.includes('testing_failed') || reason.includes('after_code_fix') || reason.includes('fix_from_testing')) return true;
    return [...(events || [])].reverse().some((event) => {
      if (event?.payload?.stage !== stageName) return false;
      const raw = String(event?.payload?.reason || '').toLowerCase();
      return raw.includes('testing_failed') || raw.includes('after_code_fix') || raw.includes('fix_from_testing');
    });
  }

  function buildWorkflowViewModel(options) {
    const {
      taskId,
      taskStatus,
      stageDefinitions,
      stageState,
      events,
      effectiveEventConfigs,
      rawEventConfigs,
      providerLabel,
      providerModelLabel,
      collapseDevLoop,
      isExecuting,
      customNodePositions,
      conversationGroups,
      conversationMessages,
    } = options || {};

    const stages = normalizeStageDefinitions(stageDefinitions);
    const groups = detectDevelopmentGroups(stages);
    const displayItems = [{ kind: 'created', id: 'created', label: 'TaskCreated' }];
    if (collapseDevLoop && groups.length) {
      for (let index = 0; index < stages.length; index += 1) {
        const group = groups.find((item) => item.startIndex === index);
        if (group) {
          displayItems.push({ kind: 'dev-group', id: group.id, group });
          index = group.endIndex;
        } else if (!groups.some((item) => index > item.startIndex && index <= item.endIndex)) {
          displayItems.push({ kind: 'stage', id: stages[index].name, stage: stages[index] });
        }
      }
    } else {
      stages.forEach((stage) => displayItems.push({ kind: 'stage', id: stage.name, stage }));
    }
    displayItems.push({ kind: 'graph-run', id: 'graph-run', label: 'GraphRun' });

    const startX = 26;
    const gapX = 26;
    const laneTopY = 84;
    const baseHeight = 320;

    const rawNodes = [];
    const processMap = buildStageProcessMap(conversationMessages);
    const graphNodeStatus = computeGraphNodeStatus({ events, taskStatus, isExecuting, stageState, stages });
    let cursorX = startX;
    displayItems.forEach((item) => {
      if (item.kind === 'created') {
        const title = '任务创建';
        const role = '系统节点';
        const meta = '流程起点';
        const size = estimateNodeSize({ title, role, meta, submeta: [{ text: '已创建', tone: 'status', status: 'done' }] });
        rawNodes.push({ id: item.id, stageType: 'created', title, role, status: 'done', metaLabel: '', meta, submeta: [{ text: '已创建', tone: 'status', status: 'done' }], editable: false, x: cursorX, y: laneTopY, width: size.width, height: size.height });
        cursorX += size.width + gapX;
        return;
      }
      if (item.kind === 'graph-run') {
        const title = '任务完成';
        const role = '系统节点';
        const meta = graphNodeStatus === 'done' ? '流程终点' : '等待收敛';
        const submeta = [{ text: nodeStatusLabel(graphNodeStatus), tone: 'status', status: graphNodeStatus }];
        const size = estimateNodeSize({ title, role, meta, submeta });
        rawNodes.push({ id: item.id, stageType: 'graph-run', title, role, status: graphNodeStatus, metaLabel: '', meta, submeta, editable: false, x: cursorX, y: laneTopY, width: size.width, height: size.height });
        cursorX += size.width + gapX;
        return;
      }
      if (item.kind === 'dev-group') {
        const status = aggregateGroupStatus(item.group.stageKeys, stageState || {});
        const size = estimateNodeSize({ title: item.group.title, role: '', meta: `${item.group.stages.length} 个阶段`, submeta: [] });
        rawNodes.push({
          id: item.id,
          stageType: 'dev-loop',
          title: item.group.title,
          role: '',
          status,
          metaLabel: '',
          meta: `${item.group.stages.length} 个阶段`,
          submeta: [],
          editable: false,
          x: cursorX,
          y: laneTopY,
          width: size.width,
          height: size.height,
          bubble: '',
        });
        cursorX += size.width + gapX;
        return;
      }
      const stage = item.stage;
      const stageCfg = (effectiveEventConfigs || {})[stage.name] || {};
      const itemState = (stageState || {})[stage.name] || {};
      const process = processMap[stage.name] || null;
      const modelText = compactNodeText(stageCfg.model || providerModelLabel?.(stageCfg.model_provider) || '未配置', '未配置');
      const roleText = primaryNodeText(stage.role || stageCfg.planned_role || '', '');
      const chips = [
        { text: `${itemState.attempts || 0}次执行`, tone: 'count' },
        { text: stageDisplayStatus(itemState), tone: 'status', status: itemState.status || 'pending' },
      ];
      const phaseText = process?.label || itemState.runtimeAlert || itemState.progressMessage || '';
      const phaseTone = process?.tone || (itemState.runtimeLevel === 'critical' ? 'error' : (itemState.runtimeLevel === 'warning' ? 'warning' : 'active'));
      const size = estimateNodeSize({ title: stage.label, role: roleText, meta: modelText, submeta: chips.slice(0, 2), phase: phaseText });
      rawNodes.push({
        id: stage.name,
        stageType: stage.stage_type,
        title: stage.label,
        role: roleText,
        status: itemState.status || 'pending',
        metaLabel: '模型',
        meta: modelText,
        phase: phaseText,
        phaseTone,
        submeta: chips.slice(0, 2),
        editable: true,
        x: cursorX,
        y: laneTopY,
        width: size.width,
        height: size.height,
        bubble: '',
      });
      cursorX += size.width + gapX;
    });
    const baseWidth = Math.max(960, cursorX + 48);

    const nodes = rawNodes.map((node) => ({ ...node, ...(customNodePositions?.[node.id] || {}) }));
    const nodeMap = Object.fromEntries(nodes.map((node) => [node.id, node]));
    const edges = [];

    for (let i = 0; i < nodes.length - 1; i += 1) {
      const fromNode = nodes[i];
      const toNode = nodes[i + 1];
      let status = 'pending';
      if (fromNode.status === 'done' && ['done', 'running', 'waiting'].includes(toNode.status)) status = 'done';
      if (fromNode.status === 'running' || toNode.status === 'running') status = 'running';
      if (['rework'].includes(fromNode.status) || ['rework'].includes(toNode.status)) status = 'rework';
      if (['error'].includes(fromNode.status) || ['error'].includes(toNode.status)) status = 'error';
      edges.push({ id: `${fromNode.id}-${toNode.id}`, status, path: computeNodePath(fromNode, toNode) });
    }

    if (!collapseDevLoop) {
      stages.forEach((stage) => {
        const node = nodeMap[stage.name];
        if (!node) return;
        const stageType = normalizeStageType(stage.stage_type);
        if (stageType === 'coding' && shouldShowSmokeLoop(stage.name, events, stageState || {})) {
          const nodeWidth = Number(node.width || NODE_MIN_WIDTH);
          const nodeHeight = Number(node.height || NODE_MIN_HEIGHT);
          edges.push({
            id: `${stage.name}-self-loop`,
            status: (stageState?.[stage.name]?.status === 'error') ? 'error' : 'rework',
            path: `M ${node.x + nodeWidth * 0.48} ${node.y + nodeHeight} C ${node.x - 10} ${node.y + 16}, ${node.x - 10} ${node.y + nodeHeight + 48}, ${node.x + nodeWidth * 0.48} ${node.y + nodeHeight * 0.72}`,
            label: '冒烟失败 → 编码修复',
            labelX: node.x - 6,
            labelY: node.y + 10,
          });
        }
        if (stageType === 'testing' && shouldShowTestingLoop(stage.name, events, stageState || {})) {
          const codingStage = relatedCodingStage(stages, stage.name);
          const codingNode = codingStage ? nodeMap[codingStage.name] : null;
          if (!codingNode) return;
          const startX = node.x + Number(node.width || NODE_MIN_WIDTH) / 2;
          const startY = node.y - 6;
          const endX = codingNode.x + Number(codingNode.width || NODE_MIN_WIDTH) / 2;
          const endY = codingNode.y - 6;
          const topY = Math.max(56, Math.min(startY, endY) - 80);
          edges.push({
            id: `${stage.name}-${codingStage.name}-rework`,
            status: (stageState?.[stage.name]?.status === 'error') ? 'error' : 'rework',
            path: `M ${startX} ${startY} C ${startX} ${topY}, ${endX} ${topY}, ${endX} ${endY}`,
            label: '测试失败 → 打回编码',
            labelX: (startX + endX) / 2 - 48,
            labelY: topY - 8,
          });
        }
      });
    }

    const overlayGroups = [];
    const seenOverlayIds = new Set();
    const addOverlayGroup = ({ id, title, meta = '', kind = 'loop', stageKeys = [], padX = 34, padTop = 72, padBottom = 58 }) => {
      if (!id || seenOverlayIds.has(id)) return;
      const groupNodes = stageKeys.map((stageKey) => nodeMap[stageKey]).filter(Boolean);
      if (groupNodes.length < 2) return;
      const minX = Math.min(...groupNodes.map((node) => Number(node.x || 0)));
      const minY = Math.min(...groupNodes.map((node) => Number(node.y || 0)));
      const maxX = Math.max(...groupNodes.map((node) => Number(node.x || 0) + Number(node.width || NODE_MIN_WIDTH)));
      const maxY = Math.max(...groupNodes.map((node) => Number(node.y || 0) + Number(node.height || NODE_MIN_HEIGHT)));
      overlayGroups.push({
        id,
        title,
        meta,
        kind,
        x: minX - padX,
        y: minY - padTop,
        width: (maxX - minX) + padX * 2,
        height: (maxY - minY) + padTop + padBottom,
      });
      seenOverlayIds.add(id);
    };

    if (!collapseDevLoop) {
      groups.forEach((group) => {
        addOverlayGroup({
          id: group.id,
          title: group.title,
          meta: '执行回环',
          kind: 'loop',
          stageKeys: group.stageKeys,
          padX: 34,
          padTop: 72,
          padBottom: 58,
        });
      });
    }

    (conversationGroups || []).forEach((group) => {
      const rawMembers = Array.isArray(group?.stage_names)
        ? group.stage_names
        : (Array.isArray(group?.stages) ? group.stages : []);
      const stageKeys = rawMembers.map((name) => String(name)).filter((name) => nodeMap[name]);
      if (stageKeys.length < 2) return;
      addOverlayGroup({
        id: `conversation:${String(group.key || group.id || group.label || stageKeys.join('|'))}`,
        title: String(group.label || group.title || group.key || '对话组'),
        meta: '对话组',
        kind: 'conversation',
        stageKeys,
        padX: 22,
        padTop: 44,
        padBottom: 34,
      });
    });

    const contentBounds = {
      minX: 18,
      minY: 26,
      maxX: baseWidth - 18,
      maxY: baseHeight - 18,
    };
    nodes.forEach((node) => {
      const left = Number(node.x || 0) - 16;
      const top = Number(node.y || 0) - 18;
      const right = Number(node.x || 0) + Number(node.width || NODE_MIN_WIDTH) + 16;
      const bottom = Number(node.y || 0) + Number(node.height || NODE_MIN_HEIGHT) + 18;
      contentBounds.minX = Math.min(contentBounds.minX, left);
      contentBounds.minY = Math.min(contentBounds.minY, top);
      contentBounds.maxX = Math.max(contentBounds.maxX, right);
      contentBounds.maxY = Math.max(contentBounds.maxY, bottom);
    });
    overlayGroups.forEach((group) => {
      contentBounds.minX = Math.min(contentBounds.minX, Number(group.x || 0) - 6);
      contentBounds.minY = Math.min(contentBounds.minY, Number(group.y || 0) - 22);
      contentBounds.maxX = Math.max(contentBounds.maxX, Number(group.x || 0) + Number(group.width || 0) + 6);
      contentBounds.maxY = Math.max(contentBounds.maxY, Number(group.y || 0) + Number(group.height || 0) + 8);
    });
    edges.forEach((edge) => {
      if (!edge.label) return;
      const labelX = Number(edge.labelX || 0);
      const labelY = Number(edge.labelY || 0);
      contentBounds.minX = Math.min(contentBounds.minX, labelX - 18);
      contentBounds.minY = Math.min(contentBounds.minY, labelY - 18);
      contentBounds.maxX = Math.max(contentBounds.maxX, labelX + 140);
      contentBounds.maxY = Math.max(contentBounds.maxY, labelY + 20);
    });

    const width = Math.max(baseWidth, Math.ceil(contentBounds.maxX + 28));
    const height = Math.max(baseHeight, Math.ceil(contentBounds.maxY + 28));

    const lastStageEvent = [...(events || [])].reverse().find((event) => event?.payload?.stage && stages.some((stage) => stage.name === event.payload.stage));
    const runningStage = stages.find((stage) => stageState?.[stage.name]?.status === 'running');
    return {
      width,
      height,
      nodes,
      edges,
      groups: overlayGroups,
      contentBounds,
      hotNodeId: lastStageEvent?.payload?.stage || runningStage?.name || '',
    };
  }

  function renderMiniMap(vm, workflowView) {
    const baseWidth = Number(vm?.width || 1220);
    const baseHeight = Number(vm?.height || 360);
    const miniWidth = 170;
    const miniHeight = 96;
    const xScale = miniWidth / baseWidth;
    const yScale = miniHeight / baseHeight;
    const scale = Number(workflowView?.scale || 1);
    const offsetX = Number(workflowView?.offsetX || 0);
    const offsetY = Number(workflowView?.offsetY || 0);
    const viewportWidth = Math.min(miniWidth, miniWidth / scale);
    const viewportHeight = Math.min(miniHeight, miniHeight / scale);
    const viewportLeft = Math.max(0, (-offsetX / scale) * xScale);
    const viewportTop = Math.max(0, (-offsetY / scale) * yScale);
    const nodes = (vm?.nodes || []).map((node) => {
      const width = Math.max(16, Number(node.width || NODE_MIN_WIDTH) * xScale);
      const height = Math.max(8, Number(node.height || NODE_MIN_HEIGHT) * yScale);
      return `<div class="workflow-minimap-node ${node.status}" style="left:${node.x * xScale}px;top:${node.y * yScale}px;width:${width}px;height:${height}px"></div>`;
    }).join('');
    return `<div class="workflow-minimap"><div class="workflow-minimap-grid"></div>${nodes}<div class="workflow-minimap-viewport" style="left:${viewportLeft}px;top:${viewportTop}px;width:${viewportWidth}px;height:${viewportHeight}px"></div></div>`;
  }

  window.MacsWorkflow = {
    DEFAULT_STAGES,
    normalizeStageType,
    normalizeStageDefinitions,
    isStageEvent,
    stageIcon,
    nodeStatusLabel,
    stageDisplayStatus,
    stageProcessLabel,
    detectDevelopmentGroups,
    estimateNodeSize,
    summarizeStages,
    computeTaskInsights,
    summarizeFailure,
    computeNodePath,
    buildWorkflowViewModel,
    renderMiniMap,
  };
})();
