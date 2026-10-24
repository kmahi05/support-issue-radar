import { NextResponse, type NextRequest } from "next/server";

import { ACCESS_COOKIE, REFRESH_COOKIE, refreshTokens, setTokenCookies } from "./lib/session";

const PUBLIC = ["/login", "/register"];

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (PUBLIC.some((p) => pathname.startsWith(p))) return NextResponse.next();

  const refresh = request.cookies.get(REFRESH_COOKIE)?.value;
  if (!refresh) return NextResponse.redirect(new URL("/login", request.url));
  if (request.cookies.get(ACCESS_COOKIE)) return NextResponse.next();

  // Access cookie expired: refresh here, where cookies can be written for the whole request.
  const pair = await refreshTokens(refresh).catch(() => null);
  if (!pair) {
    const res = NextResponse.redirect(new URL("/login", request.url));
    res.cookies.delete(REFRESH_COOKIE);
    return res;
  }
  request.cookies.set(ACCESS_COOKIE, pair.access_token);
  request.cookies.set(REFRESH_COOKIE, pair.refresh_token);
  const res = NextResponse.next({ request });
  setTokenCookies(res.cookies, pair);
  return res;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
