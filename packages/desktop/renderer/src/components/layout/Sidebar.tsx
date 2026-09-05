import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { useState } from 'react';
import {
  Activity,
  Bot,
  ClipboardCheck,
  Database,
  FileSearch,
  FileText,
  LayoutDashboard,
  Newspaper,
  PenTool,
  Scale,
  Settings,
  ShieldCheck,
  ChevronDown,
  Wrench,
} from 'lucide-react';
import logoImg from '../../assets/logo.png';

const groups = [
  {
    title: '项目',
    items: [
      { path: '/dashboard', icon: LayoutDashboard, label: '项目列表', desc: '项目与阶段状态' },
      { path: '/graph', icon: Activity, label: '全链路运行', desc: '总图工作台' },
    ],
  },
  {
    title: '高级工具',
    items: [
      { path: '/interpret', icon: FileSearch, label: '招标文件', desc: '解读与证据' },
      { path: '/generate', icon: PenTool, label: '大纲与正文', desc: '章节编辑器' },
      { path: '/qualification', icon: ClipboardCheck, label: '资格与材料', desc: '凭证比对' },
      { path: '/check', icon: ShieldCheck, label: '检查报告', desc: '规则与修复' },
      { path: '/format', icon: FileText, label: '文档输出', desc: '排版与导出' },
      { path: '/knowledge', icon: Database, label: '知识中心', desc: 'RAG 知识库' },
      { path: '/rules', icon: Scale, label: '规则审核', desc: '版本与回滚' },
      { path: '/monitor', icon: Activity, label: '运行监控', desc: '成功率与 Trace' },
      { path: '/news', icon: Newspaper, label: '资讯中心', desc: '热点与商机' },
      { path: '/workbench', icon: Bot, label: 'Agent 工作台', desc: 'AG-UI 集成面' },
    ],
  },
  {
    title: '平台',
    items: [
      { path: '/settings', icon: Settings, label: '平台设置', desc: '模型、配图与权限' },
    ],
  },
];

export default function Sidebar() {
  const location = useLocation();
  const navigate = useNavigate();
  const [advancedOpen, setAdvancedOpen] = useState(false);
  return (
    <aside style={{ width: 260, minWidth: 260, background: '#fff', display: 'flex', flexDirection: 'column', overflow: 'auto', color: '#1e293b', borderRight: '1px solid #e2e8f0' }}>
      <button onClick={() => navigate('/dashboard')} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '16px 20px 14px', border: 0, borderBottom: '1px solid #f1f5f9', background: '#fff', cursor: 'pointer', textAlign: 'left' }}>
        <img src={logoImg} alt="投标智航 / TenderPilot" style={{ width: 36, height: 36, borderRadius: 8, objectFit: 'contain' }} />
        <span>
          <strong style={{ display: 'block', fontSize: 15, color: '#0f172a' }}>投标智航</strong>
          <small style={{ display: 'block', color: '#64748b', fontSize: 11 }}>TenderPilot</small>
          <small style={{ color: '#94a3b8' }}>全流程智能招投标平台</small>
        </span>
      </button>
      <nav aria-label="主导航" style={{ padding: '8px 0' }}>
        {groups.map((group) => (
          group.title === '高级工具' ? (
            <section key={group.title}>
              <button onClick={() => setAdvancedOpen((value) => !value)} style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 20px 6px', border: 0, background: 'transparent', color: '#94a3b8', cursor: 'pointer' }}>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 10, fontWeight: 700, letterSpacing: '0.08em' }}><Wrench size={12} /> 高级工具</span>
                <ChevronDown size={13} style={{ transform: advancedOpen ? 'rotate(180deg)' : 'none', transition: 'transform 0.15s' }} />
              </button>
              {advancedOpen && group.items.map((item) => (
                <NavLink key={item.path} to={item.path} style={({ isActive }) => ({ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 20px', textDecoration: 'none', color: isActive ? '#1a56db' : '#475569', background: isActive ? '#eff6ff' : 'transparent', borderLeft: isActive ? '3px solid #1a56db' : '3px solid transparent' })}>
                  <item.icon size={17} />
                  <span><span style={{ display: 'block', fontSize: 13 }}>{item.label}</span><small style={{ color: '#94a3b8' }}>{item.desc}</small></span>
                </NavLink>
              ))}
            </section>
          ) : (
          <section key={group.title}>
            <h2 style={{ padding: '12px 20px 6px', margin: 0, fontSize: 10, fontWeight: 700, color: '#94a3b8', letterSpacing: '0.08em' }}>{group.title}</h2>
            {group.items.map((item) => (
              <NavLink key={item.path} to={item.path} style={({ isActive }) => ({ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 20px', textDecoration: 'none', color: isActive ? '#1a56db' : '#475569', background: isActive ? '#eff6ff' : 'transparent', borderLeft: isActive ? '3px solid #1a56db' : '3px solid transparent' })}>
                <item.icon size={17} />
                <span><span style={{ display: 'block', fontSize: 13 }}>{item.label}</span><small style={{ color: '#94a3b8' }}>{item.desc}</small></span>
              </NavLink>
            ))}
          </section>
          )
        ))}
      </nav>
      <div style={{ flex: 1 }} />
      <div style={{ padding: '14px 20px', borderTop: '1px solid #f1f5f9', fontSize: 10, color: '#cbd5e1', textAlign: 'center' }}>投标智航 / TenderPilot v2.0</div>
    </aside>
  );
}
