import { defineCloudflareConfig } from "@opennextjs/cloudflare";

// No incremental cache, no queue, no tag cache: every page here is either
// static or client-side polled, so there is nothing to revalidate on the edge.
export default defineCloudflareConfig();
