const expectedStartupFailure = {
  caption: '无法启动办事程序，请检查开发环境和 Python（执行办事逻辑的程序）路径。',
  header: '办事程序不可用',
}

async function collectStartupFailure(expected) {
  const caption = document.querySelector('.caption [role=status]')
  const header = document.querySelector('.topbar .main-status')
  const input = document.querySelector('.composer textarea')
  const send = document.querySelector('.composer button[type=submit]')
  const states = []
  let resolveExpectedState
  const observe = () => {
    const state = { caption: caption.textContent, header: header.textContent, input_disabled: input.disabled, send_disabled: send.disabled }
    if (JSON.stringify(states.at(-1)) !== JSON.stringify(state)) states.push(state)
    if (state.caption === expected.caption && state.header === expected.header && state.input_disabled && state.send_disabled) {
      resolveExpectedState?.()
    }
  }
  const observer = new MutationObserver(observe)
  let timeout
  try {
    for (const status of [caption, header]) observer.observe(status, { childList: true, characterData: true, subtree: true })
    for (const control of [input, send]) observer.observe(control, { attributes: true, attributeFilter: ['disabled'] })
    observe()
    const snapshotReply = await Promise.race([
      window.myrk.command({ action: 'snapshot' }).then(() => 'resolved', () => 'rejected'),
      new Promise(resolve => { timeout = setTimeout(() => resolve('no_reply'), 2000) }),
    ])
    clearTimeout(timeout)
    await new Promise(resolve => {
      resolveExpectedState = resolve
      timeout = setTimeout(resolve, 2000)
      observe()
    })
    observe()
    return { snapshot_reply: snapshotReply, states }
  } finally {
    clearTimeout(timeout)
    observer.disconnect()
  }
}

export async function observeStartupFailure(execute) {
  const startup = await execute(`(${collectStartupFailure})(${JSON.stringify(expectedStartupFailure)})`)
  const final = startup.states.at(-1)
  return {
    startup_status_observation: startup,
    startup_failure_visible: ['无法启动办事程序', '办事程序已退出', '连接恢复未完成', '与办事程序通信失败']
      .some(text => final.caption.includes(text)),
    original_startup_error: final.caption === expectedStartupFailure.caption,
    no_ready_success: startup.snapshot_reply === 'rejected' && final.header === expectedStartupFailure.header && startup.states.every(state =>
      state.header !== '准备就绪' && state.input_disabled && state.send_disabled),
  }
}
