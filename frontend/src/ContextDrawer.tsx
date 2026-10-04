import { type ReactNode, useEffect, useRef } from 'react'

type ContextDrawerProps = {
  projectId: string
  contextKind: 'role' | 'work-item'
  contextId: string
  eyebrow: string
  title: string
  onClose: () => void
  children: ReactNode
}

export default function ContextDrawer({
  projectId,
  contextKind,
  contextId,
  eyebrow,
  title,
  onClose,
  children,
}: ContextDrawerProps) {
  const closeRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    closeRef.current?.focus()

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }

    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [onClose])

  return <div className="context-drawer-backdrop" onMouseDown={onClose}>
    <aside
      className="context-drawer"
      role="dialog"
      aria-modal="true"
      aria-labelledby="context-drawer-title"
      data-context-key={`${projectId}:${contextKind}:${contextId}`}
      onMouseDown={event => event.stopPropagation()}
    >
      <header className="context-drawer-header">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h2 id="context-drawer-title">{title}</h2>
          <p className="context-identity">{projectId} · {contextId}</p>
        </div>
        <button
          ref={closeRef}
          type="button"
          className="context-drawer-close"
          aria-label="Fermer le panneau"
          onClick={onClose}
        >
          ×
        </button>
      </header>
      <div className="context-drawer-content">{children}</div>
    </aside>
  </div>
}
