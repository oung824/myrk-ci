import type { ProviderConfig } from './protocol'

export function connectionIdentity(provider: ProviderConfig): string {
  return JSON.stringify([provider.name, provider.base_url.replace(/\/+$/, ''), provider.protocol])
}
