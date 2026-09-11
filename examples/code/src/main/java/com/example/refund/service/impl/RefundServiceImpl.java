package com.example.refund.service.impl;

import com.example.refund.dto.RefundApplyDTO;
import com.example.refund.dto.RefundDetailVO;
import com.example.refund.dto.RefundWithdrawDTO;
import com.example.refund.mapper.RefundMapper;
import com.example.refund.service.RefundService;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 退款服务实现。
 */
@Service
public class RefundServiceImpl implements RefundService {

    private final RefundMapper refundMapper;

    public RefundServiceImpl(RefundMapper refundMapper) {
        this.refundMapper = refundMapper;
    }

    /**
     * 提交退款申请。
     *
     * @param dto 申请参数
     * @return 申请结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public RefundApplyVO apply(RefundApplyDTO dto) {
        RefundApplyVO vo = new RefundApplyVO();
        refundMapper.insertApply(dto);
        return vo;
    }

    /**
     * 按批次撤销退款。
     *
     * @param dto 撤销参数
     * @return 是否成功
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public Boolean withdraw(RefundWithdrawDTO dto) {
        // 按传入的退款单逐个关闭，没有校验这些单子是否属于同一个批次
        for (Long refundId : dto.getRefundIds()) {
            refundMapper.closeById(refundId);
        }
        return Boolean.TRUE;
    }

    /**
     * 查询退款详情。
     *
     * @param refundId 退款单 ID
     * @return 详情
     */
    @Override
    public RefundDetailVO detail(Long refundId) {
        RefundOrder order = refundMapper.selectById(refundId);
        RefundDetailVO vo = new RefundDetailVO();
        // 这里回填的是状态枚举编码，不是中文名
        vo.setRefundStatusName(order.getRefundStatus());
        vo.setReturnAddress(null);
        return vo;
    }
}
