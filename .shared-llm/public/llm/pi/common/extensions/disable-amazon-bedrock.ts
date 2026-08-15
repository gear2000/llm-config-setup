interface ProviderConfig {
  models: [];
}

interface ExtensionAPI {
  registerProvider(name: string, config: ProviderConfig): void;
}

export const DISABLED_PROVIDER = "amazon-bedrock";

/**
 * Remove Amazon Bedrock from Pi's effective provider registry.
 *
 * Supplying an empty model list replaces the built-in provider's catalog. This
 * keeps Bedrock out of model selection and prevents bare model aliases such as
 * "sonnet" from resolving to a Bedrock model when direct Anthropic auth is not
 * configured.
 */
export default function disableAmazonBedrock(pi: ExtensionAPI): void {
  pi.registerProvider(DISABLED_PROVIDER, { models: [] });
}
