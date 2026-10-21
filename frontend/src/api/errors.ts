/**
 * Shared API error class — imported by both client.ts and mockLayer.ts
 * so there is a single definition with no circular dependency.
 */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: unknown,
    message: string
  ) {
    super(message);
    this.name = "ApiError";
  }
}
