import type { ReactNode } from 'react'

const shapes: Record<string, ReactNode> = {
  plus: <path d="M12 5v14M5 12h14" />,
  file: <><path d="M14 3H6v18h12V7zM14 3v5h4M9 12h6M9 16h6" /></>,
  chat: <path d="M5 4h14v12H9l-4 4z" />,
  folder: <path d="M3 6h7l2 2h9v12H3z" />,
  search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></>,
  chevronDown: <path d="m6 9 6 6 6-6" />,
  chevronRight: <path d="m9 6 6 6-6 6" />,
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  activity: <path d="M3 12h4l3-7 4 14 3-7h4" />,
  check: <path d="m5 12 4 4L19 6" />,
  shield: <><path d="m12 3 8 3v6c0 4-4 7-8 9-4-2-8-5-8-9V6z" /><path d="m8 12 3 3 5-6" /></>,
  server: <><rect x="4" y="3" width="16" height="7" rx="2" /><rect x="4" y="14" width="16" height="7" rx="2" /><path d="M8 6.5h.01M8 17.5h.01M12 6.5h5M12 17.5h5" /></>,
  sliders: <><path d="M4 6h4m4 0h8M4 12h10m4 0h2M4 18h3m4 0h9" /><circle cx="10" cy="6" r="2" /><circle cx="16" cy="12" r="2" /><circle cx="9" cy="18" r="2" /></>,
  memory: <><rect x="5" y="5" width="14" height="14" rx="3" /><path d="M9 9h6v6H9zM9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3" /></>,
  import: <><path d="M14 3h5v18H5v-5M12 8l4 4-4 4M3 12h13" /></>,
}

export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  return <svg viewBox="0 0 24 24" width={size} height={size} style={{ width: size, height: size }} aria-hidden="true">{shapes[name]}</svg>
}
