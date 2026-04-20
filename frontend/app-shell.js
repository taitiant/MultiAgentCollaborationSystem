(function () {
  const body = document.body;
  if (!body || !body.classList.contains('app-page')) return;

  const sidebarHost = document.querySelector('[data-shell-sidebar]');
  if (!sidebarHost) return;

  const page = body.dataset.page || '';

  const items = [
    { key: 'home', href: '/', title: '主页', desc: '总览入口、近期任务与系统状态。' },
    { key: 'tasks', href: '/tasks.html', title: '任务中心', desc: '创建任务、挑选任务并执行流程。' },
    { key: 'models', href: '/models.html', title: '模型管理', desc: '维护凭据、模型清单与阶段绑定。' },
    { key: 'knowledge', href: '/knowledge.html', title: '知识库设置', desc: '配置向量模型、重排序模型与检索备注。' },
    { key: 'capabilities', href: '/capabilities.html', title: '能力执行', desc: '为各能力选择执行方式与主绑定。' },
    { key: 'skills', href: '/skills.html', title: '技能策略', desc: '配置各智能体的方法论、约束与偏好能力。' },
  ];

  sidebarHost.innerHTML = `
    <div class="sidebar-brand">
      <h1>MACS</h1>
      <p>控制台</p>
    </div>
    <nav class="sidebar-nav">
      ${items.map((item) => `
        <a class="nav-link ${item.key === page ? 'active' : ''}" href="${item.href}">
          <strong>${item.title}</strong>
          <span>${item.desc}</span>
        </a>
      `).join('')}
    </nav>
    <div class="sidebar-note">
      先配置模型，再创建和执行任务。
    </div>
  `;

  let backdrop = document.querySelector('.app-sidebar-backdrop');
  if (!backdrop) {
    backdrop = document.createElement('div');
    backdrop.className = 'app-sidebar-backdrop';
    document.body.appendChild(backdrop);
  }

  function closeSidebar() {
    document.body.classList.remove('sidebar-open');
  }

  function toggleSidebar() {
    document.body.classList.toggle('sidebar-open');
  }

  backdrop.addEventListener('click', closeSidebar);

  document.querySelectorAll('[data-sidebar-toggle]').forEach((button) => {
    button.addEventListener('click', toggleSidebar);
  });

  window.addEventListener('resize', () => {
    if (window.innerWidth > 980) closeSidebar();
  });
})();
