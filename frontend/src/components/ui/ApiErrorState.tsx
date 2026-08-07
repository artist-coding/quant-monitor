import Card from './Card';
import Button from './Button';
import { IconAlert, IconRefresh } from './icons';

interface Props {
  message?: string;
  onRetry?: () => void;
}

export default function ApiErrorState({ message, onRetry }: Props) {
  return (
    <Card>
      <div className="flex flex-col items-center text-center py-10">
        <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-accent-red/10 ring-1 ring-inset ring-accent-red/25 text-accent-red mb-4">
          <IconAlert size={22} />
        </div>
        <div className="text-sm font-semibold text-text-primary mb-1">数据加载失败</div>
        <div className="text-xs text-text-muted mb-5 max-w-sm leading-relaxed">{message || '后端服务暂时不可用,请检查网络或稍后重试。'}</div>
        {onRetry && (
          <Button variant="secondary" onClick={onRetry}>
            <IconRefresh size={14} />
            重试
          </Button>
        )}
      </div>
    </Card>
  );
}
