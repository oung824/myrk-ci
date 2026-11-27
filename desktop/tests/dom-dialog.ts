if (typeof HTMLDialogElement !== 'undefined') {
  const previousFocus = new WeakMap<HTMLDialogElement, Element | null>()
  HTMLDialogElement.prototype.showModal = function () {
    previousFocus.set(this, document.activeElement)
    this.setAttribute('open', '')
    this.querySelector<HTMLElement>('[autofocus], button, input, select, textarea')?.focus()
  }
  HTMLDialogElement.prototype.close = function (value = '') {
    if (!this.open) return
    this.returnValue = value
    this.removeAttribute('open')
    const target = previousFocus.get(this)
    if (target instanceof HTMLElement && target.isConnected) target.focus()
    this.dispatchEvent(new Event('close'))
  }
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || event.defaultPrevented) return
    const dialog = [...document.querySelectorAll<HTMLDialogElement>('dialog[open]')].at(-1)
    if (dialog?.dispatchEvent(new Event('cancel', { cancelable: true }))) dialog.close()
  })
}
