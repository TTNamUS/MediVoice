import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Allow cross-origin requests from the bot server in development
  async headers() {
    return [
      {
        source: "/api/:path*",
        headers: [
          { key: "Access-Control-Allow-Origin", value: "*" },
        ],
      },
    ];
  },
};

export default nextConfig;
