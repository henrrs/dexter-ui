import { createServer } from 'node:net'

/** Reserve a set of distinct ephemeral loopback ports for isolated browser tests. */
export async function freePorts(count) {
  const servers = []
  const ports = []
  try {
    for (let index = 0; index < count; index += 1) {
      const server = createServer()
      servers.push(server)
      await new Promise((resolve, reject) => {
        server.once('error', reject)
        server.listen(0, '127.0.0.1', resolve)
      })
      ports.push(server.address().port)
    }
    return ports
  } finally {
    await Promise.all(servers.map((server) => new Promise((resolve) => server.close(resolve))))
  }
}
