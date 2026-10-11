import type { ButtonHTMLAttributes, HTMLAttributes, InputHTMLAttributes, ReactNode } from 'react'

import { cn } from '../lib/utils'

// Project-owned, source-included primitives following the portable shadcn
// composition model documented by Supabase Library. No Supabase runtime,
// credentials, telemetry, or second hosted backend is added to the browser.

export function Button({ className, variant = 'default', type = 'button', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'default' | 'ghost' | 'outline' }) {
  return (
    <button
      className={cn(
        'inline-flex h-9 items-center justify-center rounded-md px-3 text-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-50',
        variant === 'default' && 'bg-emerald-500 text-black hover:bg-emerald-400',
        variant === 'outline' && 'border border-border bg-transparent text-foreground hover:bg-muted',
        variant === 'ghost' && 'text-muted-foreground hover:bg-muted hover:text-foreground',
        className,
      )}
      type={type}
      {...props}
    />
  )
}

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('rounded-lg border border-border bg-card text-card-foreground shadow-sm', className)} {...props} />
}

export function CardHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex flex-col gap-1.5 p-5', className)} {...props} />
}

export function CardTitle({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return <h3 className={cn('font-medium tracking-tight', className)} {...props} />
}

export function CardContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('p-5 pt-0', className)} {...props} />
}

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn('h-9 w-full rounded-md border border-border bg-background px-3 text-sm outline-none placeholder:text-muted-foreground focus:border-emerald-500', className)} {...props} />
}

export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'positive' | 'negative' | 'warning' }) {
  return (
    <span className={cn(
      'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
      tone === 'neutral' && 'border-border bg-muted text-muted-foreground',
      tone === 'positive' && 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
      tone === 'negative' && 'border-red-500/30 bg-red-500/10 text-red-300',
      tone === 'warning' && 'border-amber-500/30 bg-amber-500/10 text-amber-200',
    )}>{children}</span>
  )
}

export function Alert({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-4 rounded-lg border border-amber-500/30 bg-amber-500/8 p-4">
      <div className="min-w-60 flex-1"><div className="font-medium text-amber-100">{title}</div><div className="mt-1 text-sm text-muted-foreground">{children}</div></div>
      {action}
    </div>
  )
}
