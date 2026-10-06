import { type ReactNode, useEffect, useRef } from 'react'
import { nextFocusIndex } from './focusNavigation'

type ContextDrawerProps = {
  projectId: string
  contextKind: 'role' | 'work-item'
  contextId: string
  eyebrow: string
  title: string
  onClose: () => void
  children: ReactNode
}

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export default function ContextDrawer({
  projectId,
  contextKind,
  contextId,
  eyebrow,
  title,
  onClose,
  children,
}: ContextDrawerProps) {
  const drawerRef = useRef<HTMLElement | null>(null)
  const closeRef = useRef<HTMLButtonElement | null>(null)
  const onCloseRef = useRef(onClose)

  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    const previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    document.body.style.overflow = 'hidden'
    closeRef.current?.focus()

    function visibleFocusableElements() {
      if (!drawerRef.current) return []
      return Array.from(drawerRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
        .filter(element => element.getAttribute('aria-hidden') !== 'true' && element.getClientRects().length > 0)
    }

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault()
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab') return

      const focusable = visibleFocusableElements()
      const currentIndex = focusable.indexOf(document.activeElement as HTMLElement)
      const targetIndex = nextFocusIndex(currentIndex, focusable.length, event.shiftKey)
      if (targetIndex === null) return

      event.preventDefault()
      focusable[targetIndex]?.focus()
    }

    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
      previousFocus?.focus()
    }
  }, [])

  return <div className="context-drawer-backdrop" onMouseDown={() => onCloseRef.current()}>
    <aside
      ref={drawerRef}
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
          onClick={() => onCloseRef.current()}
        >
          ×
        </button>
      </header>
      <div className="context-drawer-content">{children}</div>
    </aside>
  </div>
}
