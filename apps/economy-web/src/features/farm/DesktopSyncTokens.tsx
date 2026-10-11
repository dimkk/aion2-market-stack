import { useEffect, useState } from "react";
import { Copy, KeyRound, RotateCcw, Trash2 } from "lucide-react";

import { CopyForLlm } from "../../components/CopyForLlm";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
} from "../../components/ui";
import { economyApi } from "../../lib/api";
import type { AppLocale } from "../../lib/i18n";
import type { DesktopSyncToken, SessionState } from "../../types";

const ONE_WEEK = 7 * 24 * 60 * 60;

export function DesktopSyncTokens({
  session,
  locale,
}: {
  session: SessionState;
  locale: AppLocale;
}) {
  const [tokens, setTokens] = useState<DesktopSyncToken[]>([]);
  const [label, setLabel] = useState("Desktop farm tracker");
  const [issuedToken, setIssuedToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tr = (ru: string, en: string) => (locale === "ru" ? ru : en);

  const load = async () => {
    try {
      setTokens(await economyApi.desktopSyncTokens());
      setError(null);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "desktop_sync_tokens_failed",
      );
    }
  };
  useEffect(() => {
    void load();
  }, []);

  const issue = async () => {
    setBusy(true);
    setError(null);
    setIssuedToken(null);
    setCopied(false);
    try {
      const result = await economyApi.issueDesktopSyncToken(
        label.trim(),
        ONE_WEEK,
        session.csrfToken,
      );
      setIssuedToken(result.token);
      setTokens((current) => [
        result.tokenMeta,
        ...current.filter((item) => item.id !== result.tokenMeta.id),
      ]);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "desktop_sync_token_issue_failed",
      );
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (!issuedToken) return;
    try {
      await navigator.clipboard.writeText(issuedToken);
      setCopied(true);
    } catch {
      setCopied(false);
      setError("desktop_sync_token_copy_failed");
    }
  };

  const revoke = async (tokenId: string) => {
    setBusy(true);
    setError(null);
    try {
      await economyApi.revokeDesktopSyncToken(tokenId, session.csrfToken);
      setTokens((current) =>
        current.map((item) =>
          item.id === tokenId
            ? { ...item, revokedAt: new Date().toISOString() }
            : item,
        ),
      );
      setIssuedToken(null);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "desktop_sync_token_revoke_failed",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRound className="size-4 text-emerald-300" />
          {tr("Автосинхронизация desktop tracker", "Desktop tracker auto-sync")}
        </CardTitle>
        <p className="text-sm text-muted-foreground">
          {tr(
            "Создайте отдельный farm:write token. Он показывается один раз, не хранится браузером и не даёт доступа к рынку или администрированию.",
            "Create a separate farm:write token. It is shown once, is not stored by the browser, and cannot access market or administration data.",
          )}
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && (
          <Alert
            title={tr(
              "Не удалось управлять sync token",
              "Could not manage sync token",
            )}
            action={
              <CopyForLlm
                message={`AION2 Economy desktop sync token: ${error}`}
                context={{
                  severity: "error",
                  state: "desktop-sync-token-failed",
                }}
              />
            }
          >
            {error}
          </Alert>
        )}
        {issuedToken && (
          <Alert
            title={tr(
              "Токен создан — скопируйте сейчас",
              "Token created — copy it now",
            )}
          >
            <div className="space-y-3">
              <p>
                {tr(
                  "После закрытия или обновления страницы значение больше не показывается.",
                  "The value will not be shown again after closing or refreshing the page.",
                )}
              </p>
              <div className="flex flex-wrap gap-2">
                <Button onClick={() => void copy()}>
                  <Copy className="mr-2 size-4" />
                  {copied
                    ? tr("Скопировано", "Copied")
                    : tr("Скопировать токен", "Copy token")}
                </Button>
                <Button variant="outline" onClick={() => setIssuedToken(null)}>
                  {tr("Скрыть", "Hide")}
                </Button>
              </div>
            </div>
          </Alert>
        )}
        <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
          <Input
            maxLength={64}
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder={tr("Метка устройства", "Device label")}
          />
          <Button disabled={busy || !label.trim()} onClick={() => void issue()}>
            <RotateCcw className="mr-2 size-4" />
            {tr("Создать на 7 дней", "Create for 7 days")}
          </Button>
        </div>
        <div className="overflow-auto rounded-md border border-border">
          <table className="w-full min-w-160 text-sm">
            <thead>
              <tr className="bg-muted/70 text-xs uppercase text-muted-foreground">
                <th className="p-3 text-left">{tr("Метка", "Label")}</th>
                <th className="p-3 text-left">{tr("Срок", "Expiry")}</th>
                <th className="p-3 text-left">{tr("Статус", "Status")}</th>
                <th className="p-3 text-right">{tr("Действие", "Action")}</th>
              </tr>
            </thead>
            <tbody>
              {tokens.map((token) => {
                const expired =
                  new Date(token.expiresAt).getTime() <= Date.now();
                const active = !token.revokedAt && !expired;
                return (
                  <tr key={token.id} className="border-t border-border">
                    <td className="p-3">
                      <div>{token.label || tr("Без метки", "No label")}</div>
                      <div className="text-xs text-muted-foreground">
                        {token.id}
                      </div>
                    </td>
                    <td className="p-3">
                      {new Date(token.expiresAt).toLocaleString(
                        locale === "ru" ? "ru-RU" : "en-US",
                      )}
                    </td>
                    <td className="p-3">
                      <Badge tone={active ? "positive" : "neutral"}>
                        {token.revokedAt
                          ? tr("отозван", "revoked")
                          : expired
                            ? tr("истёк", "expired")
                            : tr("активен", "active")}
                      </Badge>
                    </td>
                    <td className="p-3 text-right">
                      <Button
                        variant="outline"
                        disabled={busy || !active}
                        onClick={() => void revoke(token.id)}
                      >
                        <Trash2 className="mr-2 size-4" />
                        {tr("Отозвать", "Revoke")}
                      </Button>
                    </td>
                  </tr>
                );
              })}
              {!tokens.length && (
                <tr>
                  <td className="p-3 text-muted-foreground" colSpan={4}>
                    {tr(
                      "Активных или прежних токенов нет.",
                      "No active or previous tokens.",
                    )}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}
