const roleNames = () => (session?.roles || []).map((item) => item.role);
const currentRoleSummary = () => {
  const roles = roleNames();
  if (roles.includes('SYSTEM_ADMIN')) return '系统管理员（最高权限）';
  return roles.map(fmtRole).join('、') || '未分配岗位';
};
const capabilityRoles = {
  'signal.view':['OBSERVER','PROPOSER','REVIEWER','OPERATOR'],
  'opportunity.view':['OBSERVER','PROPOSER'],
  'proposal.view':['OBSERVER','PROPOSER','REVIEWER','OPERATOR'],
  'operations.view':['OBSERVER','OPERATOR'],
  'results.view':['OBSERVER','OPERATOR'],
  'notification.view':['OBSERVER','PROPOSER','REVIEWER','OPERATOR','TREASURY_ADMIN'],
  'system.view':['OBSERVER','REVIEWER','OPERATOR'],
  'venue.view':['OBSERVER','OPERATOR'],
  'venue.sync':['OPERATOR'],
  'capital.view':['TREASURY_ADMIN','SYSTEM_ADMIN'],
  'proposal.create':['PROPOSER'],
  'proposal.review':['REVIEWER'],
  'access.manage':['SYSTEM_ADMIN'],
};
const hasCapability = (capability) => (
  roleNames().includes('SYSTEM_ADMIN')
  || (capabilityRoles[capability] || []).some(role => roleNames().includes(role))
);
const currentWorkspaceMembership = () => (session?.workspaces || []).find(
  workspace => workspace.workspace_id === session?.active_workspace?.workspace_id
);
const routeCapability = (path) => {
  if (path === '/' || path === '/workspaces' || path === '/home' || path === '/profile/api-keys' || path === '/profile/api-access' || path === '/admin/agents') return null;
  if (path === '/capital') return 'capital.view';
  if (path === '/opportunities/defaults') return 'proposal.create';
  if (path === '/opportunities') return 'opportunity.view';
  if (path === '/webhook-signals') return 'signal.view';
  if (path === '/signals') return 'signal.view';
  if (path === '/proposals/new') return 'proposal.create';
  if (path === '/reviews') return 'proposal.review';
  if (path === '/proposals' || path.startsWith('/proposals/')) return 'proposal.view';
  if (path === '/campaigns' || path.startsWith('/campaigns/') || path === '/orders' || path === '/exceptions') return 'operations.view';
  if (path === '/accounts' || path === '/trading-mode' || path === '/team-settings') return 'venue.view';
  if (path === '/results') return 'results.view';
  if (path === '/notifications') return 'notification.view';
  if (path === '/positions' || path === '/risk') return 'system.view';
  if (path === '/venues' || path.startsWith('/venues/')) return 'venue.view';
  if (path === '/admin/users') return 'access.manage';
  return 'operations.view';
};
const capabilityLabel = (capability) => ({'signal.view':'查看信号源','opportunity.view':'查看机会','proposal.view':'查看提案','operations.view':'风险管理','results.view':'查看绩效报表','notification.view':'查看通知中心','system.view':'查看系统状态','venue.view':'查看交易账户','capital.view':'资金管理','proposal.create':'发起提案','proposal.review':'独立审核','access.manage':'成员权限管理'}[capability] || capability);
const accessRoleCatalog = [
  {role:'OBSERVER', label:'只读观察', copy:'查看机会、提案、交易任务、系统状态和交易账户；不能执行动作。'},
  {role:'PROPOSER', label:'发起提案', copy:'查看机会并创建提案；不能审核自己的提案，也不能操作交易任务。'},
  {role:'REVIEWER', label:'独立审核', copy:'独立审核冻结提案与风险恢复申请；不能发起提案、操作交易或查看资金。'},
  {role:'OPERATOR', label:'风险管理', copy:'设置和修改风险规则，执行风险检查、风险暂停、授权、减仓和对账；需独立审核的变更仍按流程审批，不自动获得资金权限。'},
  {role:'TREASURY_ADMIN', label:'资金管理', copy:'查看与管理资金数据、划转和资金对账；与风险管理职责分离。'},
  {role:'SYSTEM_ADMIN', label:'超级管理员', copy:'管理所有成员并可访问资金中心；所有资金动作仍受实时校验、最终确认和安全开关约束。'},
];
const loginDestination = () => {
  return '/';
};
