import React from "react";
import { describe, expect, it } from "vitest";

import RootLayout, { metadata } from "@/app/layout";

describe("RootLayout", () => {
  it("keeps the document and local font contract stable", () => {
    const document = RootLayout({ children: <main>content</main> });
    expect(document.type).toBe("html");
    expect(document.props.lang).toBe("en");
    expect(document.props["data-theme"]).toBe("light");

    const body = document.props.children;
    expect(body.type).toBe("body");
    expect(body.props.className).toContain("font-body");
    expect(String(metadata.title)).toContain("PolyUQuest");
  });
});
