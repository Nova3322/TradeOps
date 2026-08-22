const main = document.querySelector('#main');
const sidebar = document.querySelector('#sidebar');
const userMenu = document.querySelector('#user-menu');
const identityChip = document.querySelector('#identity-chip');
const userMenuPanel = document.querySelector('#user-menu-panel');
const userMenuName = document.querySelector('[data-user-menu-name]');
const userMenuAuth = document.querySelector('[data-user-menu-auth]');
const userMenuWorkspace = document.querySelector('[data-user-menu-workspace]');
const userMenuTeam = document.querySelector('[data-user-menu-team]');
const userMenuRole = document.querySelector('[data-user-menu-role]');
const passwordChangeForm = document.querySelector('#password-change-form');
const scopeControl = document.querySelector('#scope-control');
const scopeSwitcher = document.querySelector('#scope-switcher');
const scopeSwitcherMenu = document.querySelector('#workspace-switcher-menu');
const teamModeSelect = document.querySelector('#team-mode-select');
const environmentBadge = document.querySelector('#environment-badge');
const environmentModeValue = document.querySelector('[data-environment-mode]');
const teamModeMenu = document.querySelector('#team-mode-menu');
const preferenceSelects = new Map(
  [...document.querySelectorAll('[data-preference-select]')].map(element => [element.dataset.preferenceSelect, element]),
);
const themeColorMeta = document.querySelector('meta[name="theme-color"]');
const currentDate = document.querySelector('#current-date');
const mobileNavToggle = document.querySelector('#mobile-nav-toggle');
const desktopNavToggle = document.querySelector('#desktop-nav-toggle');
const mobileSessionSummary = document.querySelector('#mobile-session-summary');
const mobileLogoutButton = document.querySelector('#mobile-logout-button');
const sidebarDatabaseStatus = document.querySelector('#sidebar-database-status');
const navBackdrop = document.querySelector('#nav-backdrop');
const dialog = document.querySelector('#system-proposal-dialog');
const confirmDialog = document.querySelector('#confirm-dialog');
const toast = document.querySelector('#toast');
let session = null;
let sessionAuthenticationMethod = '';
let authStatus = null;
let instruments = [];
let opportunities = [];
let opportunityGroups = [];
let proposalDefaults = {configured:false, can_manage:false, data:null};
let opportunitySourceRuntime = null;
let opportunitySocket = null;
let opportunityReconnectTimer = null;
let opportunityReconnectAttempt = 0;
const OPPORTUNITY_PAGE_SIZE = 24;
let opportunityVisibleLimit = OPPORTUNITY_PAGE_SIZE;
let sessionNotice = '';
let toastTimer = null;
let authFailureActive = false;
let teamModeDropdownRequestToken = 0;
let teamModeSnapshot = null;
let mobileNavFocusFrame = null;
let mobileNavFocusToken = 0;
let sidebarDatabaseRequestToken = 0;
const REQUEST_TIMEOUT_MS = 15000;
const LANGUAGE_STORAGE_KEY = 'trading-language';
const THEME_STORAGE_KEY = 'trading-theme';
const DESKTOP_NAV_STORAGE_KEY = 'trading-desktop-nav-collapsed';
const PREFERENCE_OPTIONS = Object.freeze({
  language:Object.freeze([
    Object.freeze({value:'zh-CN', label:'中文'}),
    Object.freeze({value:'en', label:'English'}),
  ]),
  theme:Object.freeze([
    Object.freeze({value:'system', label:'跟随系统'}),
    Object.freeze({value:'light', label:'浅色'}),
    Object.freeze({value:'dark', label:'深色'}),
  ]),
});
const LEGACY_THEME_PREFERENCE_ALIASES = Object.freeze({
  auto:'system', default:'system', os:'system', 'follow-system':'system',
});
