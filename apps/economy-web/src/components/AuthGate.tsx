import { FormEvent, type ReactNode, useState } from "react";
import { Database, LockKeyhole, UserPlus } from "lucide-react";

import type { AppLocale } from "../lib/i18n";
import type { SessionState } from "../types";
import { CopyForLlm } from "./CopyForLlm";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
} from "./ui";

type Mode = "login" | "register";
const registrationEnabled =
  import.meta.env.VITE_REGISTRATION_ENABLED === "true";
const copy = {
  ru: {
    checking: "Проверяю сессию…",
    apiDown: "Auth API недоступен",
    intro:
      "Приватная аналитика рынка. Данные доступны только приглашённым аккаунтам.",
    login: "Войти",
    register: "Регистрация",
    accepted: "Регистрация принята",
    acceptedBody:
      "После одобрения администратором войдите с тем же логином и паролем.",
    username: "Логин",
    password: "Пароль (минимум 12 символов)",
    failed: "Не удалось выполнить вход",
    checkingButton: "Проверяю…",
    create: "Создать аккаунт",
    awaiting: "Аккаунт ожидает одобрения",
    awaitingBody:
      "зарегистрирован. Рыночные данные не загружались и останутся недоступны, пока администратор не выдаст роль approved.",
    logout: "Выйти",
  },
  en: {
    checking: "Checking session…",
    apiDown: "Auth API is unavailable",
    intro:
      "Private market analytics. Data is available to invited accounts only.",
    login: "Log in",
    register: "Register",
    accepted: "Registration received",
    acceptedBody:
      "After an administrator approves the account, log in with the same username and password.",
    username: "Username",
    password: "Password (12 characters minimum)",
    failed: "Authentication failed",
    checkingButton: "Checking…",
    create: "Create account",
    awaiting: "Account awaiting approval",
    awaitingBody:
      "is registered. Market data was not loaded and remains unavailable until an administrator grants the approved role.",
    logout: "Log out",
  },
} as const;

export function AuthGate({
  session,
  loading,
  error,
  locale,
  onLocaleChange,
  onAuthenticate,
  onLogout,
  children,
}: {
  session: SessionState | null;
  loading: boolean;
  error: string | null;
  locale: AppLocale;
  onLocaleChange: (locale: AppLocale) => void;
  onAuthenticate: (
    mode: Mode,
    username: string,
    password: string,
  ) => Promise<"authenticated" | "registered">;
  onLogout: () => Promise<void>;
  children: ReactNode;
}) {
  const [mode, setMode] = useState<Mode>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [registrationReceived, setRegistrationReceived] = useState(false);
  const t = copy[locale];
  const language = (
    <div className="fixed right-4 top-4 z-10 inline-flex rounded-md border border-border bg-background p-0.5 text-xs">
      <button
        className={`rounded px-2 py-1 ${locale === "ru" ? "bg-muted" : ""}`}
        onClick={() => onLocaleChange("ru")}
      >
        RU
      </button>
      <button
        className={`rounded px-2 py-1 ${locale === "en" ? "bg-muted" : ""}`}
        onClick={() => onLocaleChange("en")}
      >
        EN
      </button>
    </div>
  );
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setFormError(null);
    try {
      const result = await onAuthenticate(mode, username, password);
      setRegistrationReceived(result === "registered");
      if (result === "registered") setMode("login");
    } catch (reason) {
      setFormError(reason instanceof Error ? reason.message : "auth_failed");
    } finally {
      setSubmitting(false);
    }
  };
  if (loading)
    return (
      <div className="grid min-h-screen place-items-center bg-background text-foreground">
        {language}
        <div className="text-sm text-muted-foreground">{t.checking}</div>
      </div>
    );
  if (error && !session)
    return (
      <div className="mx-auto max-w-2xl p-8">
        {language}
        <Alert
          title={t.apiDown}
          action={
            <CopyForLlm
              message={`AION2 Economy auth: ${error}`}
              context={{ severity: "error", state: "auth-api-unavailable" }}
            />
          }
        >
          {error}
        </Alert>
      </div>
    );
  if (!session?.authenticated || session.role === "guest")
    return (
      <div className="grid min-h-screen place-items-center bg-background p-5 text-foreground">
        {language}
        <Card className="w-full max-w-md">
          <CardHeader>
            <div className="mb-3 flex size-10 items-center justify-center rounded-lg bg-emerald-500/12 text-emerald-300">
              <Database className="size-5" />
            </div>
            <CardTitle>AION2 Economy</CardTitle>
            <p className="text-sm text-muted-foreground">{t.intro}</p>
          </CardHeader>
          <CardContent>
            {registrationEnabled && (
              <div className="mb-4 grid grid-cols-2 gap-2">
                <Button
                  variant={mode === "login" ? "default" : "outline"}
                  onClick={() => setMode("login")}
                >
                  <LockKeyhole className="mr-2 size-4" />
                  {t.login}
                </Button>
                <Button
                  variant={mode === "register" ? "default" : "outline"}
                  onClick={() => setMode("register")}
                >
                  <UserPlus className="mr-2 size-4" />
                  {t.register}
                </Button>
              </div>
            )}
            <form className="space-y-3" onSubmit={submit}>
              {registrationReceived && (
                <Alert
                  title={t.accepted}
                  action={
                    <CopyForLlm
                      message="AION2 Economy: registration received and awaiting approval."
                      context={{
                        severity: "warning",
                        state: "registration-awaiting-approval",
                      }}
                    />
                  }
                >
                  {t.acceptedBody}
                </Alert>
              )}
              <Input
                required
                type="text"
                minLength={3}
                maxLength={64}
                pattern="[A-Za-z0-9][A-Za-z0-9._-]{2,63}"
                autoComplete="username"
                placeholder={t.username}
                value={username}
                onChange={(event) => setUsername(event.target.value)}
              />
              <Input
                required
                type="password"
                minLength={12}
                autoComplete={
                  mode === "login" ? "current-password" : "new-password"
                }
                placeholder={t.password}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
              {formError && (
                <Alert
                  title={t.failed}
                  action={
                    <CopyForLlm
                      message={`AION2 Economy authentication failed: ${formError}`}
                      context={{
                        severity: "error",
                        state: "authentication-failed",
                        mode,
                      }}
                    />
                  }
                >
                  {formError}
                </Alert>
              )}
              <Button className="w-full" disabled={submitting} type="submit">
                {submitting
                  ? t.checkingButton
                  : mode === "login"
                    ? t.login
                    : t.create}
              </Button>
            </form>
          </CardContent>
        </Card>
      </div>
    );
  if (session.role === "registered")
    return (
      <div className="grid min-h-screen place-items-center bg-background p-5 text-foreground">
        {language}
        <Card className="w-full max-w-lg">
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle>{t.awaiting}</CardTitle>
              <Badge tone="warning">registered</Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm text-muted-foreground">
              {session.username} {t.awaitingBody}
            </p>
            <div className="flex flex-wrap gap-2">
              <CopyForLlm
                message="AION2 Economy: account is awaiting approved role; market data was not loaded."
                context={{
                  severity: "warning",
                  state: "account-awaiting-approval",
                }}
              />
              <Button variant="outline" onClick={onLogout}>
                {t.logout}
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  return <>{children}</>;
}
