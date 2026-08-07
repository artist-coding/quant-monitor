import type { SVGProps } from 'react';

interface IconProps extends SVGProps<SVGSVGElement> {
  size?: number;
}

function base({ size = 18, ...props }: IconProps) {
  return {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.8,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    ...props,
  };
}

export const IconGrid = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="3" y="3" width="7" height="7" rx="1.5" />
    <rect x="14" y="3" width="7" height="7" rx="1.5" />
    <rect x="3" y="14" width="7" height="7" rx="1.5" />
    <rect x="14" y="14" width="7" height="7" rx="1.5" />
  </svg>
);

export const IconTarget = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="9" />
    <circle cx="12" cy="12" r="5" />
    <circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" />
  </svg>
);

export const IconStar = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M12 2.5l2.95 5.98 6.6.96-4.78 4.66 1.13 6.58L12 17.57l-5.9 3.1 1.13-6.57L2.45 9.44l6.6-.96L12 2.5z" />
  </svg>
);

export const IconHistory = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M3 12a9 9 0 1 0 2.64-6.36L3 8" />
    <path d="M3 3v5h5" />
    <path d="M12 7v5l3.5 2" />
  </svg>
);

export const IconArchive = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="3" y="4" width="18" height="5" rx="1.5" />
    <path d="M5 9v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9" />
    <path d="M9 13h6" />
  </svg>
);

export const IconActivity = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M22 12h-4l-3 8L9 4l-3 8H2" />
  </svg>
);

export const IconExchange = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M7 16V4" />
    <path d="M3 8l4-4 4 4" />
    <path d="M17 8v12" />
    <path d="M13 16l4 4 4-4" />
  </svg>
);

export const IconSettings = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </svg>
);

export const IconSearch = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="11" cy="11" r="7" />
    <path d="m21 21-4.35-4.35" />
  </svg>
);

export const IconPanelOpen = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M9 4v16" />
    <path d="m14 10-2 2 2 2" />
  </svg>
);

export const IconPanelClose = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M9 4v16" />
    <path d="m12 10 2 2-2 2" />
  </svg>
);

export const IconArrowRight = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M5 12h14" />
    <path d="m13 6 6 6-6 6" />
  </svg>
);

export const IconRefresh = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M21 12a9 9 0 1 1-2.64-6.36L21 8" />
    <path d="M21 3v5h-5" />
  </svg>
);

export const IconZap = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M13 2 4.09 12.97a.5.5 0 0 0 .4.82H11l-1 8 8.91-10.97a.5.5 0 0 0-.4-.82H13l1-8z" />
  </svg>
);

export const IconAlert = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
    <path d="M12 9v4" />
    <path d="M12 17h.01" />
  </svg>
);

export const IconCheck = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M20 6 9 17l-5-5" />
  </svg>
);

export const IconRadar = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M12 20h.01" />
    <path d="M2 8.82a15 15 0 0 1 20 0" />
    <path d="M5 12.86a10 10 0 0 1 14 0" />
    <path d="M8.5 16.43a5 5 0 0 1 7 0" />
  </svg>
);

export const IconChart = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M3 3v16a2 2 0 0 0 2 2h16" />
    <path d="m7 14 4-4 4 3 5-6" />
  </svg>
);

export const IconClock = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </svg>
);

export const IconDatabase = (p: IconProps) => (
  <svg {...base(p)}>
    <ellipse cx="12" cy="5" rx="8" ry="3" />
    <path d="M4 5v14c0 1.66 3.58 3 8 3s8-1.34 8-3V5" />
    <path d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3" />
  </svg>
);

/** 品牌标志：金底三根上升蜡烛 */
export const LogoMark = ({ size = 36, className }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 48 48" fill="none" className={className}>
    <defs>
      <linearGradient id="zge-logo-gold" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stopColor="#f8cd6a" />
        <stop offset="1" stopColor="#e09b24" />
      </linearGradient>
    </defs>
    <rect x="2" y="2" width="44" height="44" rx="12" fill="url(#zge-logo-gold)" />
    <rect x="3.25" y="3.25" width="41.5" height="41.5" rx="10.75" stroke="rgba(255,255,255,0.35)" strokeWidth="1.5" />
    {/* 蜡烛 1（低） */}
    <rect x="15.2" y="23" width="1.6" height="14.5" rx="0.8" fill="#2a1e06" />
    <rect x="13" y="27" width="6" height="7.5" rx="1.2" fill="#2a1e06" />
    {/* 蜡烛 2（中） */}
    <rect x="23.2" y="17" width="1.6" height="15.5" rx="0.8" fill="#2a1e06" />
    <rect x="21" y="20.5" width="6" height="8.5" rx="1.2" fill="#2a1e06" />
    {/* 蜡烛 3（高） */}
    <rect x="31.2" y="10" width="1.6" height="16.5" rx="0.8" fill="#2a1e06" />
    <rect x="29" y="13.5" width="6" height="9.5" rx="1.2" fill="#2a1e06" />
  </svg>
);
