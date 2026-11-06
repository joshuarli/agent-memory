declare module "node:diagnostics_channel" {
  interface Channel {
    publish(message: unknown): void;
  }
  const diagnostics: { channel(name: string): Channel };
  export default diagnostics;
}
