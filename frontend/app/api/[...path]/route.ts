import { proxyBffRequest, type BffRouteContext } from "@/lib/server/bffProxy";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request, context: BffRouteContext): Promise<Response> {
  return proxyBffRequest(request, context);
}

export async function POST(request: Request, context: BffRouteContext): Promise<Response> {
  return proxyBffRequest(request, context);
}
