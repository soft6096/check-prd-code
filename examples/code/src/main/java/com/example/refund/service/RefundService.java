package com.example.refund.service;

import com.example.refund.dto.RefundApplyDTO;
import com.example.refund.dto.RefundDetailVO;
import com.example.refund.dto.RefundWithdrawDTO;

/**
 * 退款服务。
 */
public interface RefundService {

    /**
     * 提交退款申请。
     *
     * @param dto 申请参数
     * @return 申请结果
     */
    RefundApplyVO apply(RefundApplyDTO dto);

    /**
     * 按批次撤销退款。
     *
     * @param dto 撤销参数
     * @return 是否成功
     */
    Boolean withdraw(RefundWithdrawDTO dto);

    /**
     * 查询退款详情。
     *
     * @param refundId 退款单 ID
     * @return 详情
     */
    RefundDetailVO detail(Long refundId);
}
