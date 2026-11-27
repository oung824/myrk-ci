import { join } from 'node:path'

export function configureOwnerProfile(app, profile, runtime, transport) {
  app.setPath('userData', profile)
  app.setPath('sessionData', transport === 'real' ? profile : join(runtime, 'owner/session'))
  app.commandLine.appendSwitch('disk-cache-dir', join(runtime, 'owner/cache'))
  app.commandLine.appendSwitch('disable-gpu-shader-disk-cache')
}
