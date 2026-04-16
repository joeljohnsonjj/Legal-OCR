import React from 'react';
import { Sparkles, Loader2 } from 'lucide-react';

interface AIAnalyzingAnimationProps {
  message?: string;
  size?: 'sm' | 'md' | 'lg';
}

export function AIAnalyzingAnimation({ 
  message = 'AI analyzing documents...', 
  size = 'md' 
}: AIAnalyzingAnimationProps) {
  const sizeClasses = {
    sm: 'w-4 h-4',
    md: 'w-6 h-6',
    lg: 'w-8 h-8',
  };

  return (
    <div className="flex items-center gap-2 text-red-600 animate-pulse">
      <div className="relative">
        <Sparkles className={`${sizeClasses[size]} animate-spin`} style={{ animationDuration: '2s' }} />
        <Loader2 
          className={`${sizeClasses[size]} absolute top-0 left-0 animate-spin text-red-400`} 
          style={{ animationDuration: '1s', animationDirection: 'reverse' }}
        />
      </div>
      {message && <span className="text-sm font-medium">{message}</span>}
    </div>
  );
}

