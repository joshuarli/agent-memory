import { bindings, defineConfig, exports } from "cf/config";

export default defineConfig({
	worker: {
		name: "usr-memories",
		compatibilityDate: "2026-10-30",
		compatibilityFlags: ["nodejs_compat"],
		entrypoint: "src/index.ts",
		workersDev: false,
		previewUrls: false,
		observability: {
			enabled: true,
			logs: {
				enabled: true,
				invocationLogs: true,
				headSamplingRate: 0.25,
			},
			traces: {
				enabled: true,
				headSamplingRate: 0.1,
			},
		},
		domains: [
			"memories.17166969.xyz",
		],
		tailConsumers: [{ worker: "stats-observe" }],
		env: {
			ACCESS_TEAM_DOMAIN: bindings.text("joshuarli.cloudflareaccess.com"),
			ACCESS_AUD: bindings.text("5a104e8f635a9b4d424e56de441221c443d3293ddd26c751da7e2393713bdfa6"),
			DB: bindings.d1({
				name: "usr-memories",
				id: "9870c4da-3758-4f0c-ab6d-2372eb34009b",
			}),
			BUCKET: bindings.r2({
				name: "usr-memories",
			}),
		},
		exports: {
			default: exports.worker({
				cache: {
					enabled: false,
				},
			}),
		},
	},
});
