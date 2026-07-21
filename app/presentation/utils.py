from fastapi.responses import HTMLResponse


def hx_error(content: str, target: str, reswap: str = "innerHTML") -> HTMLResponse:
    return HTMLResponse(
        content=content,
        headers={"HX-Retarget": target, "HX-Reswap": reswap},
    )
