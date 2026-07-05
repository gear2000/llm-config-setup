declare const process: {
	cwd(): string;
	env: Record<string, string | undefined>;
	exit(code?: number): never;
	kill(pid: number, signal?: string): boolean;
	stderr: { write(message: string): void };
	stdout: { write(message: string): void };
	argv: string[];
};

declare const Buffer: {
	isBuffer(value: unknown): boolean;
	from(value: string | ArrayBuffer | Uint8Array): unknown;
};

declare module "@mariozechner/pi-coding-agent" {
	export interface ExtensionContext {
		cwd?: string;
		hasUI?: boolean;
		ui: {
			notify(
				message: string,
				level?: "info" | "warning" | "error" | string,
			): void;
			setStatus?(key: string, value?: string): void;
		};
	}
	export interface ExtensionAPI {
		registerCommand(
			name: string,
			config: {
				description?: string;
				handler: (
					args: string,
					ctx: ExtensionContext,
				) => unknown | Promise<unknown>;
			},
		): void;
		registerFlag(name: string, config: Record<string, unknown>): void;
		getFlag(name: string): unknown;
		registerTool(config: Record<string, unknown>): void;
		sendMessage(
			message: { customType?: string; content: string; display?: boolean },
			options?: { deliverAs?: string; triggerTurn?: boolean },
		): void;
		appendEntry(channel: string, data: Record<string, unknown>): void;
		on(
			event: string,
			handler: (
				event: unknown,
				ctx: ExtensionContext,
			) => unknown | Promise<unknown>,
		): void;
	}
}

declare module "@sinclair/typebox" {
	export const Type: {
		Object(schema: Record<string, unknown>): Record<string, unknown>;
		String(options?: Record<string, unknown>): Record<string, unknown>;
	};
}

declare namespace NodeJS {
	interface ErrnoException extends Error {
		code?: string;
	}
}

declare module "node:fs" {
	export function readFileSync(
		path: string,
		encoding: "utf-8" | "utf8",
	): string;
	export function writeFileSync(
		path: string,
		data: string,
		encoding?: "utf-8" | "utf8",
	): void;
	export function appendFileSync(
		path: string,
		data: string,
		encoding?: "utf-8" | "utf8",
	): void;
	export function mkdirSync(
		path: string,
		options?: { recursive?: boolean },
	): void;
	export function existsSync(path: string): boolean;
	export function mkdtempSync(prefix: string): string;
	export function rmSync(path: string, options?: Record<string, unknown>): void;
	export function readdirSync(path: string): string[];
	export function renameSync(oldPath: string, newPath: string): void;
	export function statSync(path: string): {
		mtimeMs: number;
		isFile(): boolean;
		isDirectory(): boolean;
	};
	export function unlinkSync(path: string): void;
	export function createWriteStream(
		path: string,
		options?: Record<string, unknown>,
	): {
		write(chunk: string | Uint8Array): void;
		end(callback?: () => void): void;
		on(event: string, listener: (...args: unknown[]) => void): void;
	};
}

declare module "node:path" {
	export function join(...parts: string[]): string;
	export function dirname(path: string): string;
	export function resolve(...parts: string[]): string;
	export function isAbsolute(path: string): boolean;
	export function parse(path: string): {
		root: string;
		dir: string;
		base: string;
		ext: string;
		name: string;
	};
	export function basename(path: string, suffix?: string): string;
}

declare module "node:url" {
	export function fileURLToPath(url: string | URL): string;
}

declare module "node:os" {
	export function homedir(): string;
	export function tmpdir(): string;
}

declare module "node:child_process" {
	export interface ReadableTextStream {
		setEncoding(encoding: string): void;
		on(event: string, listener: (chunk: string) => void): void;
	}
	export interface ChildProcess {
		pid?: number;
		kill(signal?: string): boolean;
		unref(): void;
		on(event: "error", listener: (err: Error) => void): this;
		on(event: "close", listener: (code: number | null) => void): this;
		on(event: string, listener: (...args: unknown[]) => void): this;
		stdout?: ReadableTextStream;
		stderr?: ReadableTextStream;
	}
	export interface ChildProcessWithoutNullStreams extends ChildProcess {
		stdout: ReadableTextStream;
		stderr: ReadableTextStream;
	}
	export function spawn(
		command: string,
		args?: string[],
		options?: Record<string, unknown>,
	): ChildProcessWithoutNullStreams;
}

declare module "node:crypto" {
	export function createHash(algorithm: string): {
		update(
			data: string,
			inputEncoding?: string,
		): { digest(encoding: "hex"): string };
	};
	export function randomUUID(): string;
}
