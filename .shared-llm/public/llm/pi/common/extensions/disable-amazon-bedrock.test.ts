/// <reference lib="es2022" />
// Zero-dependency behavioral test for the Pi provider policy extension.
// Runs on Node 22.6+ via native type-stripping.
// @ts-expect-error - Node native type-stripping resolves TypeScript directly.
import * as providerPolicyModule from "./disable-amazon-bedrock.ts";

const {
  default: disableAmazonBedrock,
  DISABLED_PROVIDER,
} = providerPolicyModule;

type Registration = {
  name: string;
  config: { models: [] };
};

const registrations: Registration[] = [];
const fakePi = {
  registerProvider(name: string, config: { models: [] }): void {
    registrations.push({ name, config });
  },
};

disableAmazonBedrock(fakePi);

if (DISABLED_PROVIDER !== "amazon-bedrock") {
  throw new Error(`Unexpected disabled provider: ${DISABLED_PROVIDER}`);
}
if (registrations.length !== 1) {
  throw new Error(`Expected one provider override, got ${registrations.length}`);
}
const [registration] = registrations;
if (registration?.name !== DISABLED_PROVIDER) {
  throw new Error(`Unexpected provider registration: ${registration?.name}`);
}
if (!Array.isArray(registration.config.models) || registration.config.models.length !== 0) {
  throw new Error("Amazon Bedrock must be registered with an empty model catalog");
}

console.log("disable-amazon-bedrock: ok");
