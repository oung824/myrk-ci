import { join } from 'node:path'

export default function prototypeConfig({ desktopDirectory, dependenciesDirectory, resourceDirectory, react }) {
  const prototypeDirectory = join(desktopDirectory, 'src', 'renderer', 'prototype')
  return {
    configFile: false,
    envDir: false,
    root: prototypeDirectory,
    publicDir: false,
    cacheDir: join(resourceDirectory, 'cache', 'vite'),
    plugins: [react()],
    resolve: {
      alias: ['react', 'react-dom'].map(name => ({
        find: new RegExp(`^${name}(?=/|$)`),
        replacement: join(dependenciesDirectory, name).replaceAll('\\', '/'),
      })),
      dedupe: ['react', 'react-dom'],
    },
    optimizeDeps: {
      include: ['react', 'react-dom/client', 'react/jsx-runtime', 'react/jsx-dev-runtime'],
    },
    server: {
      host: '127.0.0.1',
      port: 5186,
      strictPort: true,
      open: false,
      fs: { strict: true, allow: [prototypeDirectory, dependenciesDirectory] },
    },
    build: {
      outDir: join(resourceDirectory, 'build'),
      emptyOutDir: true,
    },
  }
}
