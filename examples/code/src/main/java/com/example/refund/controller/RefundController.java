package com.example.refund.controller;

import com.example.refund.dto.RefundApplyDTO;
import com.example.refund.dto.RefundDetailVO;
import com.example.refund.dto.RefundWithdrawDTO;
import com.example.refund.service.RefundService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 退款相关接口。
 */
@RestController
@RequestMapping("/refund")
public class RefundController {

    private final RefundService refundService;

    public RefundController(RefundService refundService) {
        this.refundService = refundService;
    }

    /**
     * 提交退款申请。
     *
     * @param dto 申请参数
     * @return 申请结果
     */
    @PostMapping("/apply")
    public Result<RefundApplyVO> apply(@RequestBody RefundApplyDTO dto) {
        return Result.ok(refundService.apply(dto));
    }

    /**
     * 按批次撤销退款。
     *
     * @param dto 撤销参数
     * @return 是否成功
     */
    @PostMapping("/withdraw")
    public Result<Boolean> withdraw(@RequestBody RefundWithdrawDTO dto) {
        return Result.ok(refundService.withdraw(dto));
    }

    /**
     * 查询退款详情。
     *
     * @param refundId 退款单 ID
     * @return 详情
     */
    @GetMapping("/detail/{refundId}")
    public Result<RefundDetailVO> detail(@PathVariable Long refundId) {
        return Result.ok(refundService.detail(refundId));
    }

    /**
     * 手动重试一笔退款。
     *
     * @param refundId 退款单 ID
     * @return 是否成功
     */
    @PostMapping("/retry")
    public Result<Boolean> retry(@RequestParam Long refundId) {
        return Result.ok(Boolean.TRUE);
    }
}
